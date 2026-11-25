"""
feedlink/kotak_ticker.py - KotakTicker: a KiteTicker drop-in that streams prices from KOTAK NEO.
================================================================================================

25-Nov-2026 (owner): Delta Neutral and Nifty Pivot take their prices from Kotak Neo, which frees two of
Zerodha's three websocket slots for the MCX / NSE desks and the ALGO engine. Orders do NOT change -
Delta Neutral still trades on Kite; only where its prices come from moves.

One-line migration (and back):
    kws = KiteTicker(api_key, access_token)   ->   kws = KotakTicker(tag="dn")

Same callbacks (on_ticks / on_connect / on_close / on_error), same subscribe / set_mode / connect, same
MODE_* names; on_ticks gets Kite-shaped dicts keyed by the KITE instrument_token the engine already uses:
    {"instrument_token", "last_price", "exchange_timestamp", "last_trade_time", "volume_traded", "oi",
     "depth": {"buy": [{price, quantity, orders}], "sell": [...]}, "tradable", "mode"}
Kite token -> Kotak contract by (segment, name, expiry, strike, CE/PE); index by name (Nifty 50 / SENSEX).

Under it is the CE/PE engine's Kotak feed (cepe_imbalance.feed.MultiFeed): its own Kotak P login
(KOTAK_*_D - a second login does not end the others, tested 16-Oct), a worker thread off the socket, the
20-s silence watchdog (25-Nov). On top:
  - old prices replayed on a reconnect are dropped (older than the price held by > 5 s AND > 60 s old;
    an index tick more than 60 s old during market hours is always dropped - an index ticks every second)
  - while the stream is silent > 3 s in market hours, Kotak REST quotes for the index + the strikes
    nearest it, every 2 s (READ-ONLY), handed over as ordinary ticks.
"""
import datetime as dt
import logging
import threading
import time

log = logging.getLogger("feedlink.kotak")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NAMES = ("NIFTY", "SENSEX", "BANKNIFTY")
SEGS = ("nse_fo", "bse_fo")
STALE_S = 5            # a tick this much older (exchange time) than the one held ...
STALE_AGE_S = 60       # ... and this old against our clock is a replay -> dropped
REST_SILENT_S = 3.0
REST_EVERY_S = 2.0
REST_NEAREST = 24      # option contracts per underlying in the REST backup (nearest the index)


def _ist_naive(epoch: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch, IST).replace(tzinfo=None)


def _market_open(now: float) -> bool:
    d = dt.datetime.fromtimestamp(now, IST)
    return d.weekday() < 5 and "09:15" <= d.strftime("%H:%M") < "15:30"


def load_kotak(client, tag: str, names=NAMES, segs=SEGS, today: str | None = None) -> list:
    """Today's Kotak NSE / BSE F&O contracts of `names` (cepe_imbalance.instruments parsing), cached."""
    import requests
    from live_trading_options.cepe_imbalance import instruments as I
    from live_trading_options.feedlink.kite_master import DATA
    today = today or dt.datetime.now(IST).date().isoformat()
    DATA.mkdir(parents=True, exist_ok=True)
    out = []
    for seg in segs:
        cache = DATA / f"kotak_{tag}_{seg}_{today}.csv"
        if cache.exists() and cache.stat().st_size > 100_000:
            text = cache.read_text(encoding="utf-8")
        else:
            text = requests.get(I.master_url(client, seg), timeout=120).text
            cache.write_text(text, encoding="utf-8")
            for old in DATA.glob(f"kotak_{tag}_{seg}_*.csv"):
                if old != cache:
                    old.unlink(missing_ok=True)
        out += I.parse(seg, text, names, today=today)
    return out


class Mapping:
    """Kite token <-> Kotak feed key."""

    def __init__(self, master, contracts):
        from live_trading_options.feedlink.kite_master import INDEX
        self.k2k: dict = {}                       # kite token -> kotak key
        self.back: dict = {}                      # kotak key -> kite token
        self.meta: dict = {}                      # kotak key -> (name, strike) for the REST backup's choice
        for tok, (key, name) in INDEX.items():
            self.k2k[tok], self.back[key], self.meta[key] = key, tok, (name, None)
        for c in contracts:
            kt = master.token(c.seg, c.name, c.expiry, c.strike, c.kind)
            if kt:
                self.k2k[kt], self.back[c.key], self.meta[c.key] = c.key, kt, (c.name, c.strike or None)

    @staticmethod
    def is_index(key: str) -> bool:
        return not key.partition("|")[2].isdigit()


class KotakTicker:
    MODE_LTP = "ltp"
    MODE_QUOTE = "quote"
    MODE_FULL = "full"

    def __init__(self, api_key=None, access_token=None, *, tag: str = "kt", env_suffix: str = "_D",
                 sessions=None, mapping=None, feed=None, log_fn=print):
        # api_key / access_token are accepted so the constructor reads like KiteTicker's; unused.
        self.tag, self.env_suffix = tag, env_suffix
        self.sessions, self.map, self.feed = sessions, mapping, feed
        self.say = log_fn
        self.on_ticks = self.on_connect = self.on_close = self.on_error = None
        self._wanted: list = []                   # kite tokens, in order
        self._wanted_set: set = set()
        self._unknown: set = set()
        self._held: dict = {}                     # kotak key -> exch_ts of the price handed over
        self._last_px: dict = {}                  # kotak key -> last price
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._was_connected = False
        self.dropped = 0
        self.rest_polls = 0
        self.rest_active = False
        self.last_error = ""

    # ── KiteTicker-compatible API ────────────────────────────────────────────
    def subscribe(self, tokens):
        keys = []
        with self._lock:
            for t in tokens:
                t = int(t)
                if t not in self._wanted_set:
                    self._wanted_set.add(t)
                    self._wanted.append(t)
                if self.map is None:
                    continue
                k = self.map.k2k.get(t)
                if k:
                    keys.append(k)
                elif t not in self._unknown:
                    self._unknown.add(t)
                    self.say(f"  [{self.tag}] Kite token {t} has no Kotak contract - not streamed")
        if keys and self.feed is not None:
            self.feed.want(keys)

    def set_mode(self, mode, tokens):
        """Kotak sends last price + best bid / ask for every contract; there is no mode to set."""
        return None

    OPEN_TRIES = 3
    OPEN_RETRY_S = 20.0

    def connect(self, threaded=False, fallback=None, **_):
        """Open the Kotak feed (login + contract match), retrying. `fallback` (optional): a callable giving a
        real KiteTicker - used when Kotak cannot be opened after OPEN_TRIES (Delta Neutral holds real money;
        its stops must never go blind because Kotak's login is down)."""
        fallback = fallback or getattr(self, "fallback", None)
        tries = 0
        while not self._stop.is_set():
            try:
                self._open()
                break
            except Exception as e:
                tries += 1
                self.last_error = f"open: {type(e).__name__}: {e}"[:200]
                self.say(f"  [{self.tag}] Kotak feed could not start (try {tries}): {self.last_error}")
                if fallback is not None and tries >= self.OPEN_TRIES:
                    self.say(f"  [{self.tag}] falling back to the engine's own Kite ticker")
                    return self._to_kite(fallback(), threaded)
                self._stop.wait(self.OPEN_RETRY_S)
        if threaded:
            return
        while not self._stop.wait(1.0):
            pass

    def _to_kite(self, kws, threaded):
        user_connect = self.on_connect

        def on_connect(ws, resp):
            with self._lock:
                wanted = list(self._wanted)              # asked for while Kotak was being tried
            if wanted:
                ws.subscribe(wanted)
                ws.set_mode(ws.MODE_FULL, wanted)
            if user_connect:
                user_connect(ws, resp)
        kws.on_ticks, kws.on_connect, kws.on_close, kws.on_error = (self.on_ticks, on_connect,
                                                                     self.on_close, self.on_error)
        self.via_kite = kws
        self.subscribe = kws.subscribe                   # the engine's later calls go straight to Kite
        self.set_mode = kws.set_mode
        return kws.connect(threaded=threaded)

    def close(self, *_):
        self._stop.set()
        if self.feed is not None:
            self.feed.stop()

    stop = close

    def status(self) -> dict:
        f = self.feed.status() if self.feed is not None else {}
        return {**f, "via": "kotak", "dropped_old": self.dropped, "rest_polls": self.rest_polls,
                "rest_active": self.rest_active, "unknown": len(self._unknown)}

    # ── start-up ───────────────────────────────────────────────────────────
    def _open(self):
        from live_trading_options.cepe_imbalance.feed import MultiFeed
        if self.sessions is None:
            from live_trading_options.mcx_terminal import config as MC
            from live_trading_options.mcx_terminal import kotak_session
            MC.ENV_SUFFIX = self.env_suffix
            self.sessions = kotak_session.SessionHolder()
        if self.map is None:
            from live_trading_options.feedlink.kite_master import KiteMaster
            client = self.sessions.get().client
            self.map = Mapping(KiteMaster(("NFO", "BFO"), names=NAMES), load_kotak(client, self.tag))
            self.say(f"  [{self.tag}] Kotak feed: {len(self.map.k2k)} Kite tokens matched to Kotak contracts")
        if self.feed is None:
            self.feed = MultiFeed(self.sessions, self._on_kotak, on_status=self._on_status)
        with self._lock:
            wanted = list(self._wanted)
        if wanted:
            self.subscribe(wanted)
        self.feed.start()
        threading.Thread(target=self._rest_loop, daemon=True, name=f"{self.tag}-kotak-rest").start()
        self._fire(self.on_connect, {})

    def _on_status(self, st):
        up = bool(st.get("connected"))
        if self._was_connected and not up:
            self._fire(self.on_close, None, st.get("error") or "kotak feed closed")
        self._was_connected = up

    def _fire(self, fn, *args):
        if fn is None:
            return
        try:
            fn(self, *args)
        except Exception as e:                     # a consumer bug never kills the feed
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            log.exception("%s callback", self.tag)

    # ── Kotak ticks -> Kite-shaped dicts ───────────────────────────────────
    def convert(self, ticks, now: float | None = None) -> list:
        now = now or time.time()
        out = []
        with self._lock:
            for t in ticks:
                kt = self.map.back.get(t.key)
                if kt is None or not t.ltp or t.ltp <= 0:
                    continue
                ts = int(t.exch_ts or 0)
                held = self._held.get(t.key, 0)
                idx = self.map.is_index(t.key)
                if ts and held and ts < held - STALE_S and ts < now - STALE_AGE_S:
                    self.dropped += 1
                    continue
                if idx and ts and ts < now - STALE_AGE_S and _market_open(now):
                    self.dropped += 1
                    continue
                if ts:
                    self._held[t.key] = max(held, ts)
                self._last_px[t.key] = t.ltp
                when = _ist_naive(ts) if ts and abs(now - ts) <= STALE_AGE_S else _ist_naive(now)
                d = {"tradable": not idx, "mode": "full", "instrument_token": kt, "last_price": float(t.ltp),
                     "exchange_timestamp": when, "last_trade_time": when, "timestamp": when}
                if not idx:
                    d["volume_traded"] = t.volume or 0
                    d["oi"] = t.oi or 0
                    d["depth"] = {"buy": [{"price": t.bid or 0.0, "quantity": t.bid_qty or 0, "orders": 0}],
                                  "sell": [{"price": t.ask or 0.0, "quantity": t.ask_qty or 0, "orders": 0}]}
                out.append(d)
        return out

    def _on_kotak(self, batch):
        out = self.convert(batch)
        if out and self.on_ticks is not None:
            try:
                self.on_ticks(self, out)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.exception("%s on_ticks", self.tag)

    # ── REST backup while Kotak's stream is silent ─────────────────────────
    def rest_keys(self) -> list:
        """Index spots first, then per underlying the REST_NEAREST option contracts nearest its index."""
        with self._lock:
            keys = [self.map.k2k[t] for t in self._wanted if t in self.map.k2k]
            px = dict(self._last_px)
        spots = [k for k in keys if self.map.is_index(k)]
        spot_of = {self.map.meta[k][0]: px.get(k) for k in spots}
        by_name: dict = {}
        for k in keys:
            if self.map.is_index(k):
                continue
            name, strike = self.map.meta.get(k, (None, None))
            ref = spot_of.get(name)
            by_name.setdefault(name, []).append((abs((strike or 0) - ref) if ref else 0, k))
        out = list(spots)
        for rows in by_name.values():
            out += [k for _, k in sorted(rows)[:REST_NEAREST]]
        return out

    def _rest_loop(self):
        from live_trading_options.cepe_imbalance.rest_backup import RestBackup
        rb = RestBackup(None, self.feed, self.sessions)
        while not self._stop.wait(REST_EVERY_S):
            try:
                silent = time.time() - (self.feed.last_tick_at or 0) > REST_SILENT_S
                if not (silent and _market_open(time.time())):
                    if self.rest_active:
                        self.rest_active = False
                        self.say(f"  [{self.tag}] Kotak stream back - REST backup off ({self.rest_polls} polls)")
                    continue
                keys = self.rest_keys()
                if not keys:
                    continue
                if not self.rest_active:
                    self.rest_active = True
                    self.say(f"  [{self.tag}] Kotak stream silent - REST prices for {len(keys)} contracts")
                ticks = rb.poll(keys)
                self.rest_polls += 1
                if ticks:
                    self._on_kotak(ticks)
            except Exception as e:
                self.last_error = f"REST: {type(e).__name__}: {e}"[:200]
                log.warning("%s REST backup: %s", self.tag, self.last_error)
                self._stop.wait(5)
