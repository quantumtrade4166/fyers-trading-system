"""
feedlink/kite_assist.py - Kite prices as a SECOND source for the Kotak-keyed engines (25-Nov-2026).
===================================================================================================

Owner, after Kotak's stream went silent for ~33 minutes on 25-Nov: take Kite prices for the MCX / NSE
desks, their order gateways and the ALGO (CE/PE) engines. Kotak stays the main feed (its contract list,
its order book); Kite ticks for the same contracts are merged in, and whichever price is newer wins
(each consumer already drops a tick older than the one it holds - Market.apply / the desks' merge).

Zerodha allows 3 websockets per API key: Nifty ST Directional keeps its own, and ONE more is the local tick
bus (tickbus/run_bus.py) that every assist here shares through BusTicker - so all of these together cost
one Kite connection. If the bus is down the consumers simply run on Kotak alone (BusTicker keeps retrying).

  KiteAssist(resolve, emit, cap)    want(ids) -> subscribes the Kite tokens of these ids (up to `cap`)
                                    Kite tick -> emit([(id, tick dict)])  on the bus client's thread
  DualFeed(primary, assist)         the one `feed` object the engine knows: want / status / start / stop go to
                                    both, everything else to the Kotak feed
"""
import datetime as dt
import logging
import threading
import time

log = logging.getLogger("feedlink.kite")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def kite_epoch(d) -> int:
    """Kite's exchange_timestamp (naive IST datetime) -> epoch seconds; 0 if absent."""
    if isinstance(d, dt.datetime):
        return int((d if d.tzinfo else d.replace(tzinfo=IST)).timestamp())
    return 0


def best(tick) -> tuple:
    """(bid, ask, bid_qty, ask_qty) from a Kite FULL tick; Nones when absent / empty."""
    d = tick.get("depth") or {}
    b = (d.get("buy") or [{}])[0] or {}
    a = (d.get("sell") or [{}])[0] or {}
    bid, ask = b.get("price") or None, a.get("price") or None
    return (bid, ask, (b.get("quantity") if bid else None), (a.get("quantity") if ask else None))


class KiteAssist:
    def __init__(self, resolve, emit, cap: int = 600, tag: str = "kite", ticker=None):
        self.resolve, self.emit, self.cap, self.tag = resolve, emit, cap, tag
        self.ticker = ticker
        self.by_tok: dict = {}            # kite token -> our id
        self.unknown = 0
        self.over_cap = 0
        self.ticks = 0
        self.last_tick_at = 0.0
        self.connected = False
        self.last_error = ""
        self._lock = threading.Lock()

    # ── engine side ────────────────────────────────────────────────────────
    def want(self, ids) -> int:
        new = []
        with self._lock:
            known = set(self.by_tok.values())
            for i in ids:
                if i in known:
                    continue
                try:
                    kt = self.resolve(i)
                except Exception:
                    kt = None
                if not kt:
                    self.unknown += 1
                    continue
                if kt in self.by_tok:
                    continue
                if len(self.by_tok) >= self.cap:
                    self.over_cap += 1
                    continue
                self.by_tok[kt] = i
                known.add(i)
                new.append(kt)
        if new and self.ticker is not None:
            try:
                self.ticker.subscribe(new)
                self.ticker.set_mode(self.ticker.MODE_FULL, new)
            except Exception as e:
                self.last_error = f"subscribe: {e}"[:200]
        return len(new)

    def start(self):
        if self.ticker is None:
            from live_trading_options.tickbus.bus_client import BusTicker
            self.ticker = BusTicker(log=lambda *a: None)
            with self._lock:
                toks = list(self.by_tok)
            if toks:                                     # queued until the bus answers; re-sent on reconnect
                self.ticker.subscribe(toks)
                self.ticker.set_mode(self.ticker.MODE_FULL, toks)
        self.ticker.on_ticks = self._on_ticks
        self.ticker.on_connect = lambda ws, r: self._up(True)
        self.ticker.on_close = lambda ws, c, r: self._up(False)
        self.ticker.on_error = lambda ws, c, r: setattr(self, "last_error", str(r)[:200])
        self.ticker.connect(threaded=True)

    def stop(self):
        try:
            self.ticker and self.ticker.close()
        except Exception:
            pass

    def alive(self, max_age: float = 5.0) -> bool:
        return self.connected and time.time() - self.last_tick_at < max_age

    def status(self) -> dict:
        return {"connected": self.connected, "subscribed": len(self.by_tok), "cap": self.cap,
                "ticks": self.ticks, "last_tick_age": round(time.time() - self.last_tick_at, 1) if self.last_tick_at else None,
                "unknown": self.unknown, "over_cap": self.over_cap, "error": self.last_error}

    # ── bus side ───────────────────────────────────────────────────────────
    def _up(self, v: bool):
        if v != self.connected:
            log.warning("%s: Kite tick bus %s", self.tag, "connected" if v else "disconnected")
        self.connected = v

    def _on_ticks(self, ws, ticks):
        rows = []
        for t in ticks:
            i = self.by_tok.get(t.get("instrument_token"))
            if i is not None and (t.get("last_price") or 0) > 0:
                rows.append((i, t))
        if not rows:
            return
        self.ticks += len(rows)
        self.last_tick_at = time.time()
        try:
            self.emit(rows)
        except Exception:
            log.exception("%s emit", self.tag)


class DualFeed:
    """Kotak feed (primary) + Kite assist behind one feed object."""

    def __init__(self, primary, assist):
        self.primary, self.assist = primary, assist

    def want(self, keys):
        keys = list(keys)
        n = self.primary.want(keys)
        self.assist.want(keys)
        return n

    def want_depth(self, tokens):
        tokens = list(tokens)
        self.assist.want(tokens)
        return self.primary.want_depth(tokens)

    def status(self) -> dict:
        st = self.primary.status()
        return {**st, "kite": self.assist.status()} if isinstance(st, dict) else st

    def start(self):
        self.primary.start()
        try:
            self.assist.start()
        except Exception as e:                           # Kite is the extra; Kotak runs regardless
            log.warning("Kite assist did not start: %s", e)

    def stop(self):
        self.assist.stop()
        self.primary.stop()

    def __getattr__(self, name):
        return getattr(self.primary, name)


# ── builders ────────────────────────────────────────────────────────────────
def cepe_resolver(universe, master):
    """CE/PE engine ids are Kotak feed keys ("nse_fo|123", "nse_cm|Nifty 50")."""
    from live_trading_options.feedlink.kite_master import INDEX_BY_KEY

    def resolve(key):
        if key.lower() in INDEX_BY_KEY:
            return INDEX_BY_KEY[key.lower()]
        c = universe.by_key.get(key)
        return master.token(c.seg, c.name, c.expiry, c.strike, c.kind) if c else None
    return resolve


def cepe_emitter(market, on_ticks):
    from live_trading_options.cepe_imbalance.feed import Tick

    def emit(rows):
        now = time.time()
        out = []
        for key, t in rows:
            bid, ask, bq, aq = best(t)
            q = market.quote(key)
            ltp = float(t["last_price"])
            if q is not None and q.ltp == ltp and (bid is None or (q.bid == bid and q.ask == ask)):
                continue                                 # the engine already holds this exact price
            out.append(Tick(key, ltp, bid, ask, bq, aq, t.get("volume_traded"), t.get("oi"),
                            kite_epoch(t.get("exchange_timestamp")) or int(now), now))
        if out:
            on_ticks(out)
    return emit


def for_cepe(universe, market, on_ticks, exchanges, cap: int, tag: str):
    from live_trading_options.feedlink.kite_master import KiteMaster
    return KiteAssist(cepe_resolver(universe, KiteMaster(exchanges)), cepe_emitter(market, on_ticks), cap=cap, tag=tag)


def desk_resolver(universe, master):
    """Desk / gateway ids are Kotak tokens (int)."""
    def resolve(tok):
        c = universe.by_token.get(tok)
        return master.token(c.seg, c.name, c.expiry, c.strike, c.kind) if c else None
    return resolve


def desk_emitter(last, on_ticks):
    """-> the desk's tuples (token, ltp, bid, ask, bid_qty, ask_qty, volume, oi, prev_close, ltq,
    last_update_epoch, net_chg, depth). Quantities / depth stay Kotak's (Kotak units); Kite gives price,
    bid, ask and time."""
    def emit(rows):
        now = time.time()
        out = []
        for tok, t in rows:
            bid, ask, _, _ = best(t)
            ltp = float(t["last_price"])
            cur = last.get(tok)
            if cur and cur[1] == ltp and (bid is None or (cur[2] == bid and cur[3] == ask)):
                continue
            out.append((tok, ltp, bid, ask, None, None, None, None, None, None,
                        kite_epoch(t.get("exchange_timestamp")) or int(now), None, None))
        if out:
            on_ticks(out)
    return emit


def for_desk(universe, last, on_ticks, exchanges, cap: int, tag: str):
    from live_trading_options.feedlink.kite_master import KiteMaster
    return KiteAssist(desk_resolver(universe, KiteMaster(exchanges)), desk_emitter(last, on_ticks), cap=cap, tag=tag)


def desk_dual(feed, universe, last, on_ticks, desk: str, tag: str, cap: int | None = None):
    """MCX / NSE desk or its order gateway: wrap its Kotak feed with the Kite assist.
    TERMINAL_KITE_ASSIST=0 turns it off; any failure here leaves the Kotak feed alone."""
    import os
    if os.getenv("TERMINAL_KITE_ASSIST", "1") != "1" or desk not in ("MCX", "NSE"):
        return feed
    try:
        ex = ("MCX",) if desk == "MCX" else ("NFO", "BFO")
        cap = cap or int(os.getenv("TERMINAL_KITE_CAP", "600"))
        assist = for_desk(universe, last, on_ticks, ex, cap=cap, tag=tag)
        assist.want([p.spot.token for p in universe.products.values() if getattr(p, "spot", None)])
        return DualFeed(feed, assist)
    except Exception as e:
        log.warning("%s: Kite assist off (Kotak only): %s", tag, e)
        return feed
