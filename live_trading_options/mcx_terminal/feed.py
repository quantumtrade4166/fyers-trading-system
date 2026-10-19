"""
mcx_terminal/feed.py — Kotak's NEW market feed ("native_batch"), always-on client.
==================================================================================

Kotak retired the old HS feed (mlhsm) on 15-Sep-2026. The new one (Kotak "Market Data
WebSocket — Developer Guide", 2026-09):
  URL    data.feedUrl from the login validate response (https -> wss)
  auth   first JSON frame {user: UCC, auth: sid, format: "native_batch", source: "NEOTRADEAPI"}
         -> 1117 ok (carries price dividers per exchange) | 1120 failed
  sub    {"event": "subscribeScrips", "inputtoken": "mcx_fo|123,mcx_fo|456", "ack_symbol": false}
  data   binary, little-endian, packed; one frame = a batch of packets, each starting with its
         own uint16 total length; route by header level byte (1 lite, 2/4/8 market picture)

The server keeps no subscriptions across connections, so a reconnect re-auths and replays them.
One thread owns the socket; ticks go out through `on_ticks(list_of_tick_tuples)`.

Tick tuple:  (token, ltp, bid, ask, bid_qty, ask_qty, volume, oi, prev_close, ltq, last_update_epoch,
              net_chg, depth)   depth = ([(price, qty)] bids, [(price, qty)] asks) on DEPTH packets, else None
The screen gets the first 12 fields; depth stays in the engine (paper fills walk it).
"""
import json
import logging
import struct
import threading
import time

import websocket                                   # websocket-client

from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.feed")

EXCH = {1: "nse_cm", 2: "nse_fo", 3: "cde_fo", 4: "nse_com", 5: "bse_cm", 6: "bse_fo",
        7: "bse_cd", 8: "bse_co", 9: "mcx_fo", 10: "ncd_co"}

# Market Picture fixed body (guide §7.3): offsets from the start of the packet incl. 9-byte header
MP = struct.Struct("<HHbBBBB I q q q q q I I I I I q I I I I h i I d i I I I I I B I")
LITE = struct.Struct("<HHbBBBB I q I q I i i I B I")
ROW = struct.Struct("<q i i")
assert MP.size == 144 and LITE.size == 54


def decode(frame: bytes, div: dict) -> list:
    """One binary batch frame -> list of tick tuples (see module doc). Non-price packets skipped."""
    out = []
    off, n = 0, len(frame)
    while off + 2 <= n:
        size = struct.unpack_from("<H", frame, off)[0]
        if size < 9 or off + size > n:
            break                                   # truncated tail: skip
        code, ex, level = struct.unpack_from("<HbB", frame, off + 2)
        d = div.get(EXCH.get(ex, ""), 100) or 100
        if code == 7208:
            if level == 1 and size >= LITE.size:
                (_, _, _, _, _, _, _, tok, ltt, ltp, ltq, close, _chgp, chg, _lot, _pr, _mu) = LITE.unpack_from(frame, off)
                out.append((tok, ltp / d, None, None, None, None, None, None, None, ltq, ltt, chg / d, None))
            elif level in (2, 4, 8) and size >= MP.size:
                f = MP.unpack_from(frame, off)
                tok, vol, ltt, lut = f[7], f[10], f[11], f[12]
                close, ltp, ltq = f[14], f[17], f[18]
                nbuy, nsell, oi, chg = f[21], f[22], f[25], f[27]
                if level == 4:
                    nbuy, nsell = 1, 1
                rows = []
                p = off + MP.size
                end = off + size
                while len(rows) < nbuy + nsell and p + ROW.size <= end:   # stop at the packet end
                    q, pr, _ = ROW.unpack_from(frame, p)
                    rows.append((pr / d, q))
                    p += ROW.size
                bids, asks = rows[:nbuy], rows[nbuy:nbuy + nsell]
                bid, bq = bids[0] if bids else (None, None)
                ask, aq = asks[0] if asks else (None, None)
                depth = (bids, asks) if level == 8 else None
                out.append((tok, ltp / d, bid, ask, bq, aq, vol, oi, close / d, ltq, lut or ltt, chg / d, depth))
        off += size
    return out


class KotakFeed:
    """Always-on feed. `want(tokens)` adds subscriptions (kept across reconnects)."""

    def __init__(self, sessions, on_ticks, on_status=None):
        self.sessions = sessions                   # kotak_session.SessionHolder
        self.on_ticks = on_ticks
        self.on_status = on_status or (lambda s: None)
        self.div = {}
        self.wanted: set[int] = set()
        self.sent: set[int] = set()
        self.depth_wanted: set[int] = set()
        self.ws = None
        self.connected = False
        self.authed = False
        self.ticks = 0
        self.last_tick_at = 0.0
        self.connects = 0
        self.last_error = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._force_login = False

    # ── public ──────────────────────────────────────────────────────────────
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="KotakFeed").start()

    def stop(self):
        self._stop.set()
        try:
            self.ws and self.ws.close()
        except Exception:
            pass

    def want(self, tokens):
        """Subscribe these tokens (no-op for ones already wanted). Respects the 3000 budget."""
        with self._lock:
            new = [t for t in tokens if t not in self.wanted]
            room = C.SUB_BUDGET - len(self.wanted)
            if len(new) > room:
                log.warning("subscription budget: %d wanted, room for %d", len(new), room)
                new = new[:max(room, 0)]
            self.wanted.update(new)
        if new and self.authed:
            self._send_subs(new)

    def want_depth(self, tokens):
        """5-level depth for these tokens (they are also subscribed as scrips first)."""
        self.want(tokens)
        with self._lock:
            new = [t for t in tokens if t not in self.depth_wanted and t in self.wanted]
            self.depth_wanted.update(new)
        if new and self.authed:
            self._send_subs(new, "subscribeDepth")

    def status(self) -> dict:
        return {"connected": self.connected, "authed": self.authed, "subscribed": len(self.sent),
                "wanted": len(self.wanted), "depth": len(self.depth_wanted), "ticks": self.ticks, "last_tick_at": self.last_tick_at,
                "connects": self.connects, "error": self.last_error}

    # ── internals ───────────────────────────────────────────────────────────
    def _send_subs(self, toks, event="subscribeScrips"):
        toks = list(toks)
        for i in range(0, len(toks), C.SUB_CHUNK):
            part = toks[i:i + C.SUB_CHUNK]
            try:
                self.ws.send(json.dumps({"event": event,
                                         "inputtoken": ",".join(f"mcx_fo|{t}" for t in part)}))
                if event == "subscribeScrips":
                    with self._lock:
                        self.sent.update(part)
            except Exception as e:
                self.last_error = f"subscribe: {e}"
                log.warning("subscribe failed: %s", e)

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                s = self.sessions.get(force=self._force_login)
                self._force_login = False
                self.authed = False
                self.sent = set()
                self.connects += 1
                def on_open(ws, s=s):
                    ws.send(json.dumps({"user": s.ucc, "auth": s.sid, "format": "native_batch",
                                        "source": "NEOTRADEAPI", "sdk_version": 1.1, "sdk_date": "2026-10-19"}))
                    log.info("feed socket open, auth sent")

                self.ws = websocket.WebSocketApp(
                    s.feed_url, on_open=on_open,
                    on_message=self._on_message, on_error=self._on_error, on_close=self._on_close)
                self.connected = True
                self.on_status(self.status())
                t0 = time.time()
                self.ws.run_forever(ping_interval=15, ping_timeout=8)
                if time.time() - t0 > 60:
                    backoff = 1.0                  # it was a healthy session; retry fast
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("feed loop: %s", self.last_error)
            self.connected = self.authed = False
            self.on_status(self.status())
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 10.0)

    def _on_message(self, ws, msg):
        if isinstance(msg, (bytes, bytearray)) and msg[:1] == b"{":
            msg = msg.decode("utf-8", "replace")    # a JSON control frame delivered as bytes
        if isinstance(msg, (bytes, bytearray)):
            ticks = decode(msg, self.div)
            if ticks:
                self.ticks += len(ticks)
                self.last_tick_at = time.time()
                self.on_ticks(ticks)
            return
        try:
            j = json.loads(msg)
        except ValueError:
            log.info("feed text (not json): %s", str(msg)[:200])
            return
        mc = j.get("message_code")
        if mc not in (1109,):
            log.info("feed control message %s: %s", mc, {k: v for k, v in j.items() if k != "exchanges"})
        if mc in (1117, 1119):
            for ex, v in (j.get("exchanges") or {}).items():
                self.div[ex] = v.get("divider", 100) or 100
            if mc == 1117 and not self.authed:
                self.authed = True
                if j.get("format") != "native_batch":
                    self.last_error = f"server format {j.get('format')} (expected native_batch)"
                    log.error(self.last_error)
                with self._lock:
                    toks = list(self.wanted)
                    dtoks = list(self.depth_wanted)
                self._send_subs(toks)
                if dtoks:
                    self._send_subs(dtoks, "subscribeDepth")
                log.info("feed authed; subscribed %d tokens", len(toks))
                self.on_status(self.status())
        elif mc == 1120:
            self.last_error = "feed auth failed: re-login"
            self._force_login = True
            log.warning("feed auth failed (1120): %s", str(j)[:160])

    def _on_error(self, ws, e):
        self.last_error = f"{type(e).__name__}: {e}"[:200]
        log.warning("feed error: %s", self.last_error)

    def _on_close(self, ws, code=None, reason=None):
        self.connected = self.authed = False
