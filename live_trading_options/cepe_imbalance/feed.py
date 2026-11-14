"""
cepe_imbalance/feed.py — ONE Kotak market-feed connection for NSE F&O, BSE F&O, MCX and index spot.
===================================================================================================

Same Kotak "native_batch" feed as the MCX terminal (packet layouts imported from mcx_terminal.feed,
which is NOT modified), but every tick is keyed "seg|token" because tokens repeat across segments.

Verified on the VPS 2026-11-02 (probe_kotak_universe.py):
  - nse_fo, bse_fo, mcx_fo option/future ticks AND both index spots arrive on one connection
  - index spot subscribe = {"event": "subscribeScrips", "inputtoken": "nse_cm|Nifty 50,bse_cm|SENSEX"}
    -> index packets, code 7207, 87 bytes; last price = int32 at byte 29 / divider (other fields unused)
  - option ticks come as level-4 market picture (best bid/ask + qty)

Tick tuple out:  Tick(key, ltp, bid, ask, bid_qty, ask_qty, volume, oi, exch_ts, recv_ts)
"""
import collections
import json
import logging
import struct
import threading
import time
from typing import NamedTuple

import websocket                                   # websocket-client

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.mcx_terminal.feed import EXCH, LITE, MP, ROW

log = logging.getLogger("cepe.feed")

INDEX_CODE = 7207
SCRIP_CODE = 7208
INDEX_LTP_AT = 29


class Tick(NamedTuple):
    key: str
    ltp: float
    bid: float | None
    ask: float | None
    bid_qty: int | None
    ask_qty: int | None
    volume: int | None
    oi: int | None
    exch_ts: int            # exchange epoch seconds (whole seconds)
    recv_ts: float          # our receive time (epoch, sub-second)


def decode(frame: bytes, div: dict, index_names: dict, recv_ts: float | None = None) -> list:
    """One binary batch frame -> [Tick]. `index_names` maps (seg, packet name) -> feed key for spots."""
    recv_ts = recv_ts or time.time()
    out = []
    off, n = 0, len(frame)
    while off + 2 <= n:
        size = struct.unpack_from("<H", frame, off)[0]
        if size < 9 or off + size > n:
            break
        code, ex, level = struct.unpack_from("<HbB", frame, off + 2)
        seg = EXCH.get(ex, "")
        d = div.get(seg, 100) or 100
        if code == INDEX_CODE and size >= 65:
            ltp = struct.unpack_from("<i", frame, off + INDEX_LTP_AT)[0] / d
            ts = struct.unpack_from("<I", frame, off + 33)[0]
            name = frame[off + 65: off + size].strip(b"\x00 ").decode("ascii", "replace")
            key = index_names.get((seg, name.lower()))
            if key and ltp > 0:
                out.append(Tick(key, ltp, None, None, None, None, None, None, ts, recv_ts))
        elif code == SCRIP_CODE:
            if level == 1 and size >= LITE.size:
                f = LITE.unpack_from(frame, off)
                tok, ltt, ltp = f[7], f[8], f[9]
                out.append(Tick(f"{seg}|{tok}", ltp / d, None, None, None, None, None, None, ltt, recv_ts))
            elif level in (2, 4, 8) and size >= MP.size:
                f = MP.unpack_from(frame, off)
                tok, vol, ltt, lut, ltp = f[7], f[10], f[11], f[12], f[17]
                nbuy, nsell, oi = f[21], f[22], f[25]
                if level == 4:
                    nbuy, nsell = 1, 1
                rows, p, end = [], off + MP.size, off + size
                while len(rows) < nbuy + nsell and p + ROW.size <= end:
                    q, pr, _ = ROW.unpack_from(frame, p)
                    rows.append((pr / d, q))
                    p += ROW.size
                bids, asks = rows[:nbuy], rows[nbuy:nbuy + nsell]
                bid, bq = bids[0] if bids else (None, None)
                ask, aq = asks[0] if asks else (None, None)
                out.append(Tick(f"{seg}|{tok}", ltp / d, bid or None, ask or None, bq, aq, vol, oi, lut or ltt, recv_ts))
        off += size
    return out


class MultiFeed:
    """Always-on feed. `want(keys)` subscribes "seg|token" / "nse_cm|Nifty 50" keys (kept across reconnects)."""

    def __init__(self, sessions, on_ticks, on_status=None):
        self.sessions = sessions                   # mcx_terminal.kotak_session.SessionHolder-like
        self.on_ticks = on_ticks
        self.on_status = on_status or (lambda s: None)
        self.div: dict = {}
        self.index_names: dict = {}                # (seg, lower name) -> key
        self.wanted: list = []                     # ordered, unique
        self._wanted_set: set = set()
        self.sent: set = set()
        self.ws = None
        self.connected = self.authed = False
        self.ticks = 0
        self.last_tick_at = 0.0
        self.connects = 0
        self.last_error = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._force_login = False
        self._frames = collections.deque()         # (recv_ts, raw frame) from the socket thread
        self._has_frames = threading.Event()
        self.batches = 0
        self.backlog_max = 0.0                     # worst wait of a frame before processing (s)
        self.slowest = 0.0                         # slowest on_ticks batch (s)
        self.work_s = 0.0
        self._lag_logged = 0.0

    # ── public ──────────────────────────────────────────────────────────────
    def start(self):
        threading.Thread(target=self._work, daemon=True, name="CepeTicks").start()
        threading.Thread(target=self._run, daemon=True, name="CepeFeed").start()

    def stop(self):
        self._stop.set()
        try:
            self.ws and self.ws.close()
        except Exception:
            pass

    def want(self, keys) -> int:
        """Subscribe these keys; returns how many were new. Respects the token budget."""
        with self._lock:
            new = [k for k in dict.fromkeys(keys) if k not in self._wanted_set]
            room = C.SUB_BUDGET - len(self.wanted)
            if len(new) > room:
                log.warning("subscription budget: %d new, room for %d", len(new), room)
                new = new[:max(room, 0)]
            for k in new:
                self._wanted_set.add(k)
                self.wanted.append(k)
                seg, _, rest = k.partition("|")
                if not rest.isdigit():
                    self.index_names[(seg, rest.lower())] = k
        if new and self.authed:
            self._send(new)
        return len(new)

    def status(self) -> dict:
        return {"connected": self.connected, "authed": self.authed, "subscribed": len(self.sent),
                "wanted": len(self.wanted), "ticks": self.ticks, "last_tick_at": self.last_tick_at,
                "connects": self.connects, "error": self.last_error,
                "queued": len(self._frames), "batches": self.batches, "backlog_max_s": round(self.backlog_max, 2),
                "slowest_batch_s": round(self.slowest, 3), "work_s": round(self.work_s, 1)}

    # ── internals ───────────────────────────────────────────────────────────
    def _send(self, keys):
        keys = list(keys)
        for i in range(0, len(keys), C.SUB_CHUNK):
            part = keys[i:i + C.SUB_CHUNK]
            try:
                self.ws.send(json.dumps({"event": "subscribeScrips", "inputtoken": ",".join(part)}))
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

                self.ws = websocket.WebSocketApp(s.feed_url, on_open=on_open, on_message=self._on_message,
                                                 on_error=self._on_error, on_close=self._on_close)
                self.connected = True
                self.on_status(self.status())
                t0 = time.time()
                self.ws.run_forever(ping_interval=15, ping_timeout=8)
                if time.time() - t0 > 60:
                    backoff = 1.0
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("feed loop: %s", self.last_error)
            self.connected = self.authed = False
            self.on_status(self.status())
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 10.0)

    # 13-Nov-2026: the socket thread used to run decode + on_ticks (market update, quote recording and the
    # strategy evaluation of all 16 books, under the engine lock) for EVERY frame. In busy bursts (19:00 IST
    # US data) it fell behind, Kotak's pong waited in the backlog past ping_timeout and the socket was
    # dropped: 127 reconnects on 13-Nov, each a 15-60 s hole. Now the socket thread only stamps and queues
    # the raw frame; a worker decodes everything waiting and hands it to on_ticks as ONE batch (tick order
    # kept, every tick still applied and recorded; each book is evaluated once per batch, not per frame).
    def _queue_frame(self, msg):
        self._frames.append((time.time(), msg))
        self._has_frames.set()

    def _work(self):
        while not self._stop.is_set():
            self._has_frames.wait(1.0)
            self._has_frames.clear()
            while self._frames:
                batch = []
                lag = 0.0
                while self._frames:
                    ts, raw = self._frames.popleft()
                    lag = max(lag, time.time() - ts)
                    batch.extend(decode(raw, self.div, self.index_names, ts))
                self.backlog_max = max(self.backlog_max, lag)
                self.batches += 1
                if not batch:
                    continue
                self.ticks += len(batch)
                self.last_tick_at = time.time()
                t0 = time.perf_counter()
                try:
                    self.on_ticks(batch)
                except Exception:
                    log.exception("on_ticks failed")
                dt_ = time.perf_counter() - t0
                self.work_s += dt_
                if dt_ > self.slowest:
                    self.slowest = dt_
                if lag > 2.0 and time.time() - self._lag_logged > 30:
                    self._lag_logged = time.time()
                    log.warning("tick processing behind by %.1f s (batch of %d ticks took %.2f s)", lag, len(batch), dt_)

    def _on_message(self, ws, msg):
        if isinstance(msg, (bytes, bytearray)) and msg[:1] == b"{":
            msg = msg.decode("utf-8", "replace")
        if isinstance(msg, (bytes, bytearray)):
            self._queue_frame(msg)
            return
        try:
            j = json.loads(msg)
        except ValueError:
            return
        mc = j.get("message_code")
        if mc in (1117, 1119):
            for ex, v in (j.get("exchanges") or {}).items():
                self.div[ex] = v.get("divider", 100) or 100
            if mc == 1117 and not self.authed:
                self.authed = True
                with self._lock:
                    keys = list(self.wanted)
                self._send(keys)
                log.info("feed authed; subscribed %d keys", len(keys))
                self.on_status(self.status())
        elif mc == 1120:
            self.last_error = "feed auth failed: re-login"
            self._force_login = True
            log.warning("feed auth failed (1120)")
        elif mc not in (1109,):
            log.info("feed control %s: %s", mc, str({k: v for k, v in j.items() if k != "exchanges"})[:200])

    def _on_error(self, ws, e):
        self.last_error = f"{type(e).__name__}: {e}"[:200]
        log.warning("feed error: %s", self.last_error)

    def _on_close(self, ws, code=None, reason=None):
        self.connected = self.authed = False
