"""
live/ws_feed.py — Delta Exchange India's public push feed, for the fast path.
=============================================================================

The engine gets the WHOLE option chain from one REST call every few seconds. That
is the right tool for choosing strikes: 58 contracts with greeks, IV and both
sides of the touch, in a single request, with no subscription to manage as spot
drifts and no socket to stall silently.

It is the wrong tool for two things:

  1. STOP DETECTION. A 5-second poll means a stop can be noticed up to 5 seconds
     late. On BTC that is real money.
  2. A LIVE SCREEN. A price that steps once every 5 seconds does not read as
     live, and this tab is meant to sit beside the NIFTY one.

So this is the fast path, and it is deliberately NARROW: the underlying index
plus whichever contracts are currently held. Two to four symbols, not the chain.
Subscriptions are re-issued when the held legs change, which is only at entry, at
an adjustment, or at a stop-out.

Public channels, no authentication, no account — the same feed anyone can read:

    spot_price   .DEXBTUSD                 several ticks a second
    mark_price   MARK:C-BTC-79400-090926   price + best bid + best ask

Everything here is defensive. A dead socket must never take the engine with it:
the reader runs on its own daemon thread, every callback is wrapped, and the
engine treats this as an OPTIONAL accelerator — `mark()` returns None when the
feed is cold and the caller falls back to the REST chain. The strategy stays
correct with the socket permanently down; it is just slower to notice a stop.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import time
import threading

WS_URL = "wss://socket.india.delta.exchange"
SPOT_INDEX = ".DEXBTUSD"

# If no message arrives for this long the socket is considered dead and rebuilt.
# Delta pushes spot several times a second, so silence this long is unambiguous.
_STALE_SECONDS = 30
_RECONNECT_MIN, _RECONNECT_MAX = 2, 60


class WSFeed:
    """Live spot + per-contract marks. Thread-safe; start() returns immediately."""

    def __init__(self, on_tick=None):
        self._lock = threading.Lock()
        self._spot = None
        self._spot_at = 0.0
        self._marks: dict = {}          # symbol -> {"mark","bid","ask","at"}
        self._want: set = set()         # contract symbols we should be subscribed to
        self._subscribed: set = set()
        self._ws = None
        self._stop = False
        self._last_msg = 0.0
        self._connected = False
        self._connects = 0
        self._msgs = 0
        self._on_tick = on_tick         # optional callback, must never raise
        self._thread = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run_forever, daemon=True,
                                        name="btc-ws")
        self._thread.start()
        return self

    def stop(self):
        self._stop = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # ── what the engine asks for ─────────────────────────────────────────
    def track(self, symbols):
        """Set the contracts to follow. Cheap to call every poll — it only acts
        when the set actually changes, which is at entry, adjustment or stop."""
        want = {s for s in symbols if s}
        with self._lock:
            if want == self._want:
                return
            self._want = want
        self._resubscribe()

    def spot(self):
        with self._lock:
            if self._spot is None or (time.time() - self._spot_at) > _STALE_SECONDS:
                return None
            return self._spot

    def mark(self, symbol: str):
        """Latest pushed mark for one contract, or None if cold/stale. None is a
        real answer — the caller falls back to the REST chain rather than acting
        on a price that may be a minute old."""
        with self._lock:
            m = self._marks.get(symbol)
            if not m or (time.time() - m["at"]) > _STALE_SECONDS:
                return None
            return m["mark"]

    def quote(self, symbol: str):
        with self._lock:
            m = self._marks.get(symbol)
            if not m or (time.time() - m["at"]) > _STALE_SECONDS:
                return None
            return dict(m)

    def status(self) -> dict:
        with self._lock:
            age = (time.time() - self._last_msg) if self._last_msg else None
            return {
                "connected": self._connected,
                "live": bool(self._connected and age is not None and age < _STALE_SECONDS),
                "last_msg_age": round(age, 1) if age is not None else None,
                "tracking": sorted(self._want),
                "reconnects": max(0, self._connects - 1),
                "messages": self._msgs,
                "spot": self._spot,
            }

    # ── socket ───────────────────────────────────────────────────────────
    def _payload(self, symbols) -> dict:
        chans = [{"name": "spot_price", "symbols": [SPOT_INDEX]}]
        if symbols:
            chans.append({"name": "mark_price",
                          "symbols": [f"MARK:{s}" for s in sorted(symbols)]})
        return {"type": "subscribe", "payload": {"channels": chans}}

    def _resubscribe(self):
        ws = self._ws
        if ws is None or not self._connected:
            return                      # the next connect subscribes from scratch
        with self._lock:
            want = set(self._want)
        try:
            ws.send(json.dumps(self._payload(want)))
            with self._lock:
                self._subscribed = want
        except Exception:
            pass                        # the reconnect loop will resubscribe

    def _run_forever(self):
        """Connect, read, and rebuild on any failure — forever. Backs off so a
        sustained outage does not hammer the exchange."""
        try:
            import websocket
        except Exception as e:
            print(f"  [btc-ws] websocket-client unavailable, fast path disabled: {e}",
                  flush=True)
            return

        delay = _RECONNECT_MIN
        while not self._stop:
            try:
                self._ws = websocket.create_connection(WS_URL, timeout=20)
                self._connected = True
                self._connects += 1
                self._last_msg = time.time()
                with self._lock:
                    want = set(self._want)
                    self._subscribed = want
                self._ws.send(json.dumps(self._payload(want)))
                delay = _RECONNECT_MIN
                self._read_loop()
            except Exception as e:
                if not self._stop:
                    print(f"  [btc-ws] {type(e).__name__}: {e} — reconnecting in {delay}s",
                          flush=True)
            finally:
                self._connected = False
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass
                self._ws = None
            if self._stop:
                return
            time.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX)

    def _read_loop(self):
        self._ws.settimeout(_STALE_SECONDS)
        while not self._stop:
            raw = self._ws.recv()       # raises on close/timeout -> reconnect
            if not raw:
                raise ConnectionError("socket closed")
            self._last_msg = time.time()
            self._msgs += 1
            try:
                m = json.loads(raw)
            except Exception:
                continue
            self._absorb(m)

    def _absorb(self, m: dict):
        t = m.get("type")
        now = time.time()
        if t == "spot_price" and m.get("symbol") == SPOT_INDEX:
            p = _f(m.get("price"))
            if p:
                with self._lock:
                    self._spot, self._spot_at = p, now
        elif t == "mark_price":
            sym = (m.get("symbol") or "")
            if sym.startswith("MARK:"):
                sym = sym[5:]
            p = _f(m.get("price"))
            if p is None or not sym:
                return
            with self._lock:
                self._marks[sym] = {"mark": p, "bid": _f(m.get("best_bid")),
                                    "ask": _f(m.get("best_ask")), "at": now}
        else:
            return
        if self._on_tick:
            try:
                self._on_tick()
            except Exception:
                pass                    # a UI writer must never kill the reader


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
