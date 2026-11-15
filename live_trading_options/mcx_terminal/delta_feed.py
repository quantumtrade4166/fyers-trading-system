"""
mcx_terminal/delta_feed.py — Delta Exchange India's PUBLIC websocket for the BTC desk (no key, no account).
==========================================================================================================

Same public face as feed.KotakFeed (want / want_depth / start / stop / status, ticks out through
on_ticks(list of tick tuples)), so the engine treats both alike. Channels (measured 15-Nov-2026, 20 BTC
options near the money, 15 s):
  l1_orderbook  best bid / ask + sizes        ~1 update/s per contract    -> bid, ask, bid_qty, ask_qty
  v2/ticker     last trade, 24h open, size, OI ~1 per 5 s per contract     -> ltp, prev_close (24h open), volume, oi
  l2_orderbook  full book (depth)             ~1 per 1-2 s                -> depth (5 levels) for held contracts
  spot_price    .DEXBTUSD index               several a second            -> the chain's underlying
Quantities are CONTRACTS (1 = 0.001 BTC), prices USD.

Tick tuple (feed.py): (token, ltp, bid, ask, bid_qty, ask_qty, volume, oi, prev_close, ltq, last_update_epoch,
                       net_chg, depth)   None = "this message does not carry it" (the engine keeps the old value)
"""
import json
import logging
import threading
import time

log = logging.getLogger("mcx.delta_feed")

WS_URL = "wss://socket.india.delta.exchange"
SPOT_INDEX = ".DEXBTUSD"
STALE_S = 30                    # spot ticks several times a second: this much silence = dead socket
RECONNECT_MIN, RECONNECT_MAX = 2, 60
DEPTH_LEVELS = 5


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    x = _f(v)
    return None if x is None else int(round(x))


def convert(m: dict, tok_of: dict, spot_token: int) -> list:
    """One Delta message -> [tick tuple] (empty for anything that is not a price for a wanted contract)."""
    t = m.get("type")
    if t == "spot_price":
        if m.get("symbol") != SPOT_INDEX:
            return []
        p = _f(m.get("price"))
        return [(spot_token, p, None, None, None, None, None, None, None, None, int(time.time()), None, None)] if p else []
    tok = tok_of.get(m.get("symbol") or "")
    if tok is None:
        return []
    if t == "l1_orderbook":
        ts = _i(m.get("timestamp"))
        return [(tok, None, _f(m.get("best_bid")), _f(m.get("best_ask")), _i(m.get("bid_qty")), _i(m.get("ask_qty")),
                 None, None, None, None, ts // 1_000_000 if ts else int(time.time()), None, None)]
    if t == "v2/ticker":
        ltp, op = _f(m.get("close")), _f(m.get("open"))
        q = m.get("quotes") or {}
        ts = _i(m.get("timestamp"))
        oi = _i(m.get("oi_contracts"))
        return [(tok, ltp, _f(q.get("best_bid")), _f(q.get("best_ask")), _i(q.get("bid_size")), _i(q.get("ask_size")),
                 _i(m.get("size")), oi, op, None, ts // 1_000_000 if ts else int(time.time()),
                 round(ltp - op, 2) if ltp is not None and op is not None else None, None)]
    if t == "l2_orderbook":
        bids = [(_f(r.get("limit_price")), _i(r.get("size"))) for r in (m.get("buy") or [])[:DEPTH_LEVELS]]
        asks = [(_f(r.get("limit_price")), _i(r.get("size"))) for r in (m.get("sell") or [])[:DEPTH_LEVELS]]
        bids = [x for x in bids if x[0] is not None and x[1]]
        asks = [x for x in asks if x[0] is not None and x[1]]
        b, a = (bids[0] if bids else (None, None)), (asks[0] if asks else (None, None))
        return [(tok, None, b[0], a[0], b[1], a[1], None, None, None, None, int(time.time()), None, (bids, asks))]
    return []


class DeltaFeed:
    """Always-on public feed. `want(tokens)` adds contracts (kept across reconnects)."""

    def __init__(self, universe, on_ticks, on_status=None, spot_token=None):
        self.sym_of = {c.token: c.symbol for c in universe.by_token.values() if c.kind in ("CE", "PE", "FUT")}
        self.tok_of = {s: t for t, s in self.sym_of.items()}
        self.spot_token = spot_token
        self.on_ticks = on_ticks
        self.on_status = on_status or (lambda s: None)
        self.wanted: set[int] = set()
        self.depth_wanted: set[int] = set()
        self.ws = None
        self.connected = False
        self.ticks = 0
        self.last_tick_at = 0.0
        self.connects = 0
        self.last_error = ""
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()

    # ── public ──────────────────────────────────────────────────────────────
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="DeltaFeed").start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self.ws and self.ws.close()
        except Exception:
            pass

    def add_contracts(self, contracts):
        """Contracts Delta listed during the day (delta_instruments.merge): known from now on."""
        for c in contracts:
            if c.kind in ("CE", "PE", "FUT"):
                self.sym_of[c.token] = c.symbol
                self.tok_of[c.symbol] = c.token

    def want(self, tokens):
        with self._lock:
            new = [t for t in tokens if t in self.sym_of and t not in self.wanted]
            self.wanted.update(new)
        if new:
            self._subscribe(new, depth=False)

    def want_depth(self, tokens):
        self.want(tokens)
        with self._lock:
            new = [t for t in tokens if t in self.sym_of and t not in self.depth_wanted]
            self.depth_wanted.update(new)
        if new:
            self._subscribe(new, depth=True)

    def status(self) -> dict:
        return {"connected": self.connected, "authed": self.connected, "subscribed": len(self.wanted) if self.connected else 0,
                "wanted": len(self.wanted), "depth": len(self.depth_wanted), "ticks": self.ticks,
                "last_tick_at": self.last_tick_at, "connects": self.connects, "error": self.last_error}

    # ── internals ───────────────────────────────────────────────────────────
    def _payload(self, toks, depth: bool, spot: bool) -> dict:
        syms = sorted(self.sym_of[t] for t in toks)
        chans = []
        if spot:
            chans.append({"name": "spot_price", "symbols": [SPOT_INDEX]})
        if syms:
            if depth:
                chans.append({"name": "l2_orderbook", "symbols": syms})
            else:
                chans += [{"name": "l1_orderbook", "symbols": syms}, {"name": "v2/ticker", "symbols": syms}]
        return {"type": "subscribe", "payload": {"channels": chans}}

    def _send(self, payload) -> bool:
        ws = self.ws
        if ws is None or not self.connected:
            return False                           # the next connect subscribes everything
        try:
            with self._send_lock:
                ws.send(json.dumps(payload))
            return True
        except Exception as e:
            self.last_error = f"subscribe: {e}"
            log.warning("subscribe failed: %s", e)
            return False

    def _subscribe(self, toks, depth: bool):
        self._send(self._payload(toks, depth, spot=False))

    def _run(self):
        import websocket                           # websocket-client
        delay = RECONNECT_MIN
        while not self._stop.is_set():
            try:
                self.ws = websocket.create_connection(WS_URL, timeout=20)
                self.connected = True
                self.connects += 1
                with self._lock:
                    want, deep = set(self.wanted), set(self.depth_wanted)
                self._send(self._payload(want, False, spot=True))
                if deep:
                    self._send(self._payload(deep, True, spot=False))
                log.info("Delta feed connected (#%d): %d contracts, %d with depth", self.connects, len(want), len(deep))
                self.on_status(self.status())
                delay = RECONNECT_MIN
                self._read()
            except Exception as e:
                if not self._stop.is_set():
                    self.last_error = f"{type(e).__name__}: {e}"
                    log.warning("Delta feed: %s - reconnecting in %ss", self.last_error, delay)
            finally:
                self.connected = False
                try:
                    self.ws and self.ws.close()
                except Exception:
                    pass
                self.ws = None
                self.on_status(self.status())
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, RECONNECT_MAX)

    def _read(self):
        self.ws.settimeout(STALE_S)
        while not self._stop.is_set():
            raw = self.ws.recv()                   # raises on close / timeout -> reconnect
            if not raw:
                raise ConnectionError("socket closed")
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("type") == "error" or (m.get("type") == "subscriptions" and m.get("error")):
                self.last_error = str(m)[:200]
                log.warning("Delta feed message: %s", self.last_error)
                continue
            ticks = convert(m, self.tok_of, self.spot_token)
            if ticks:
                self.ticks += len(ticks)
                self.last_tick_at = time.time()
                try:
                    self.on_ticks(ticks)
                except Exception:
                    log.exception("on_ticks")
