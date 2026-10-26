"""
mcx_terminal/recorder.py — the day recorder behind the double-click replay files.
==================================================================================

Records exactly what the engine sends the screen — every tick (12-field tuple, stamped with the
engine's receive time) plus every screen event (meta, chain definitions, book, risk, status,
settings, MTM points) — so a replay drives the SAME terminal code with the SAME messages.

Always on, no switch, no badge:
  * the last RING_S seconds are kept in memory (compressed 5-second chunks);
  * the day's file starts at the FIRST ORDER of the day (the ring is written first, so the
    replay opens a little before it) and then grows until the engine stops;
  * a day without orders writes nothing.

Speed: the engine's hot path does ONE list append per tick (sub-microsecond). Packing (marshal,
fast C) + compression (zlib, which releases Python's GIL) + the disk write happen on a background
thread every CHUNK_S seconds, so neither tick delivery nor order sending waits for the recorder.

File  data/replays/raw_YYYY-MM-DD.mcxr : a sequence of frames  [uint32 length][zlib(marshal(chunk))]
chunk = (t0_ms, [marshal(tick slice), ...], [marshal(event slice), ...])
        tick  = (t_ms, tok, ltp, bid, ask, bq, aq, vol, oi, pc, ltq, lut, chg)
        event = (t_ms, kind, payload)
"""
import collections
import datetime as dt
import logging
import marshal
import struct
import threading
import time
import zlib

from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.recorder")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CHUNK_S = 5.0
RING_S = 15 * 60
SLICE = 400            # ticks packed per GIL hold (~0.05 ms); the writer yields between slices
REPLAY_DIR = C.DATA / "replays"


def now_ms() -> int:
    return int(time.time() * 1000)


class DayRecorder:
    def __init__(self, day: str | None = None, folder=None):
        self.day = day or dt.datetime.now(IST).date().isoformat()
        self.folder = folder or REPLAY_DIR
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / f"raw_{self.day}.mcxr"
        # hot-path buffers: deque.append / popleft are atomic, so the writer can drain while the
        # feed thread appends and no tick is ever lost between the two
        self.ticks = collections.deque()
        self.events = collections.deque()
        self.ring = collections.deque(maxlen=int(RING_S / CHUNK_S) + 1)
        self.active = self.path.exists()           # a restart mid-day keeps appending to today's file
        self._activate_header = None
        self._lock = threading.Lock()              # guards activation vs the writer, never the hot path
        self._wlock = threading.Lock()             # one flush at a time (writer thread vs on-demand download)
        self._stop = threading.Event()
        self.stats = {"chunks": 0, "bytes": 0, "ticks": 0, "pack_ms_max": 0.0}
        self._t = threading.Thread(target=self._writer, daemon=True, name="DayRecorder")
        self._t.start()

    # ── hot path (feed thread / event loop) ─────────────────────────────────
    def tick(self, row: tuple):
        """row = (t_ms, tok, ltp, ...). One list append: safe across threads under the GIL."""
        self.ticks.append(row)

    def event(self, kind: str, payload):
        self.events.append((now_ms(), kind, payload))

    # ── activation (first order of the day) ─────────────────────────────────
    def activate(self, header_fn):
        """header_fn() -> dict with everything a replay needs at its start (meta, chain defs, last ticks,
        book, risk, settings, mtm). Called once; the writer flushes ring + header + live chunks."""
        with self._lock:
            if self.active:
                return
            self._activate_header = header_fn
            self.active = True
        log.info("replay recording ACTIVE for %s -> %s", self.day, self.path.name)

    # ── background writer ───────────────────────────────────────────────────
    def _pack(self, chunk) -> bytes:
        """chunk = (t0, [marshal blob per tick slice], marshal blob of events). The slices were packed
        one at a time with a yield in between; this only joins + compresses (zlib releases the GIL)."""
        t = time.perf_counter()
        raw = marshal.dumps(chunk)                 # joins already-packed byte strings: a memcpy
        z = zlib.compress(raw, 6)
        ms = (time.perf_counter() - t) * 1000
        if ms > self.stats["pack_ms_max"]:
            self.stats["pack_ms_max"] = round(ms, 2)
        return z

    def _drain(self, dq) -> tuple:
        """Take everything queued so far, packing SLICE rows per lock hold and yielding in between,
        so the feed thread / event loop never waits more than a few hundredths of a millisecond."""
        n = len(dq)
        blobs, first = [], None
        while n > 0:
            k = min(n, SLICE)
            rows = [dq.popleft() for _ in range(k)]
            if first is None:
                first = rows[0][0]
            blobs.append(marshal.dumps(rows))
            n -= k
            time.sleep(0)                          # yield the GIL to the engine
        return first, blobs

    def _append(self, frames):
        with open(self.path, "ab") as f:
            for z in frames:
                f.write(struct.pack("<I", len(z)))
                f.write(z)
                self.stats["bytes"] += 4 + len(z)

    def flush(self):
        with self._wlock:
            self._flush()

    def _flush(self):
        n_ticks = len(self.ticks)
        t_first, tblobs = self._drain(self.ticks)
        e_first, eblobs = self._drain(self.events)
        if not tblobs and not eblobs and not self._activate_header:
            return
        t0 = t_first or e_first or now_ms()
        z = self._pack((t0, tblobs, eblobs))
        self.stats["ticks"] += n_ticks
        with self._lock:
            hdr_fn, self._activate_header = self._activate_header, None
            if hdr_fn is not None:
                frames = list(self.ring)           # the 15 minutes before the first order
                self.ring.clear()
                try:
                    hdr = hdr_fn()
                except Exception:
                    log.exception("replay header failed")
                    hdr = {}
                frames.append(self._pack((now_ms(), [], [marshal.dumps([(now_ms(), "header", hdr)])])))
                frames.append(z)
                self._append(frames)
                self.stats["chunks"] += len(frames)
            elif self.active:
                self._append([z])
                self.stats["chunks"] += 1
            else:
                self.ring.append(z)

    def _writer(self):
        while not self._stop.wait(CHUNK_S):
            try:
                self.flush()
            except Exception:
                log.exception("recorder flush")

    def stop(self):
        self._stop.set()
        try:
            self.flush()
        except Exception:
            log.exception("recorder final flush")


def read_raw(path):
    """-> (ticks, events) for the whole day file (builder side)."""
    ticks, events = [], []
    data = open(path, "rb").read()
    off = 0
    while off + 4 <= len(data):
        n = struct.unpack_from("<I", data, off)[0]
        off += 4
        if off + n > len(data):
            break                                  # torn last frame (engine stopped mid-write)
        t0, tk, ev = marshal.loads(zlib.decompress(data[off:off + n]))
        for b in tk:                               # each element: a marshal blob of one tick slice
            ticks.extend(marshal.loads(b) if isinstance(b, bytes) else [b])
        for b in ev:
            events.extend(marshal.loads(b) if isinstance(b, bytes) else [b])
        off += n
    ticks.sort(key=lambda r: r[0])
    events.sort(key=lambda e: e[0])
    return ticks, events
