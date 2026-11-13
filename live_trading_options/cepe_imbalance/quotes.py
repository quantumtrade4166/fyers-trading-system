"""
cepe_imbalance/quotes.py — record every quote tick the engine sees, so a day can be replayed EXACTLY.
====================================================================================================

Owner 12-Nov-2026: a Supertrend-gated GOLD what-if could only be estimated, because no full chain prices
were saved (history keeps the ATM pair only). This keeps every tick of every watched contract: the strategy
grid around ATM, the chain window, futures / index spot and anything held.

File  data/quotes/{date}/{name}.qrec  — frames  [uint32 length][zlib(JSON chunk)]
      chunk = {"meta": {key: [symbol, kind, expiry, strike]}  (contracts first seen in this chunk),
               "rows": [[t, key, ltp, bid, ask, bid_qty, ask_qty, volume, oi], ...]}   t = receive epoch s
Read back with read(date, name) -> (meta, rows).

Speed: the feed thread does one deque append per tick. Grouping, JSON, zlib and the disk write run on a
background thread every FLUSH_S seconds. A locked file (backup copy, scanner) is retried, never raised.
"""
import collections
import datetime as dt
import json
import logging
import struct
import threading
import time
import zlib

from live_trading_options.cepe_imbalance import config as C

log = logging.getLogger("cepe.quotes")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
FLUSH_S = 2.0


def _day(t: float) -> str:
    return dt.datetime.fromtimestamp(t, IST).date().isoformat()


class QuoteRecorder:
    def __init__(self, universe, folder=None, start=True):
        self.u = universe
        self.folder = folder or (C.DATA / "quotes")
        self.q = collections.deque()
        self.seen: dict[tuple, set] = {}              # (date, name) -> keys whose meta is already written
        self.stats = {"rows": 0, "bytes": 0, "frames": 0, "errors": 0}
        self._stop = threading.Event()
        self._wlock = threading.Lock()
        if start:
            threading.Thread(target=self._loop, daemon=True, name="CepeQuotes").start()

    # hot path (feed thread)
    def add(self, name: str, key: str, q, t: float | None = None):
        self.q.append((t or time.time(), name, key, q.ltp, q.bid, q.ask, q.bid_qty, q.ask_qty, q.volume, q.oi))

    def _meta(self, key):
        c = self.u.by_key.get(key)
        return [c.symbol, c.kind, c.expiry, c.strike] if c else [key, "SPOT", "", 0]

    def flush(self):
        with self._wlock:
            n = len(self.q)
            if not n:
                return
            groups: dict[tuple, list] = {}
            for _ in range(n):
                t, name, key, *v = self.q.popleft()
                groups.setdefault((_day(t), name), []).append([round(t, 3), key, *v])
            for (day, name), rows in groups.items():
                seen = self.seen.setdefault((day, name), set())
                meta = {}
                for r in rows:
                    if r[1] not in seen:
                        seen.add(r[1])
                        meta[r[1]] = self._meta(r[1])
                z = zlib.compress(json.dumps({"meta": meta, "rows": rows}, separators=(",", ":")).encode(), 6)
                if self._append(day, name, z):
                    self.stats["rows"] += len(rows)
                else:                                   # could not write: forget the meta so it is re-sent
                    seen.difference_update(meta)

    def _append(self, day, name, z) -> bool:
        d = self.folder / day
        for i in range(40):
            try:
                d.mkdir(parents=True, exist_ok=True)
                with open(d / f"{name}.qrec", "ab") as f:
                    f.write(struct.pack("<I", len(z)) + z)
                self.stats["bytes"] += 4 + len(z)
                self.stats["frames"] += 1
                return True
            except PermissionError:
                time.sleep(0.05)
            except Exception:
                log.exception("quote write %s %s", day, name)
                break
        self.stats["errors"] += 1
        log.warning("quote frame dropped for %s %s (file locked)", day, name)
        return False

    def _loop(self):
        while not self._stop.wait(FLUSH_S):
            try:
                self.flush()
            except Exception:
                log.exception("quote flush")

    def stop(self):
        self._stop.set()
        try:
            self.flush()
        except Exception:
            log.exception("quote final flush")


def iter_window(date: str, name: str, t_from: float, t_to: float, keep=None, folder=None):
    """Stream rows with t_from <= t < t_to without parsing the whole day: each frame's first timestamp is
    peeked from the decompressed text, so frames entirely outside the window skip the JSON parse of their
    rows (their contract details are still collected). keep(key, meta_entry) -> bool filters contracts.
    Yields (meta_so_far, row). Rows come in recording order (frames are written in time order)."""
    p = (folder or (C.DATA / "quotes")) / date / f"{name}.qrec"
    if not p.exists():
        return
    meta: dict = {}
    allowed: dict = {}
    with open(p, "rb") as f:
        while True:
            head = f.read(4)
            if len(head) < 4:
                break
            n = struct.unpack("<I", head)[0]
            z = f.read(n)
            if len(z) < n:
                break                                   # torn last frame
            txt = zlib.decompress(z).decode()
            i = txt.find(',"rows":')
            m = json.loads(txt[8:i]) if txt.startswith('{"meta":') and i > 0 else {}
            if m:
                meta.update(m)
            j = i + len(',"rows":[[')
            k = txt.find(",", j)
            try:
                t_first = float(txt[j:k])
            except ValueError:
                continue                                # empty frame
            if t_first >= t_to:
                break
            if t_first < t_from - 10:                   # a frame spans ~2 s: peek its last row cheaply
                lb = txt.rfind("[")
                try:
                    t_last = float(txt[lb + 1:txt.find(",", lb)])
                except ValueError:
                    t_last = t_first
                if t_last < t_from:
                    continue
            for r in json.loads(txt[i + len(',"rows":'):-1]):
                if not (t_from <= r[0] < t_to):
                    continue
                key = r[1]
                ok = allowed.get(key)
                if ok is None:
                    ok = allowed[key] = (keep is None) or bool(keep(key, meta.get(key)))
                if ok:
                    yield meta, r


def read(date: str, name: str, folder=None):
    """-> (meta {key: [symbol, kind, expiry, strike]}, rows [[t, key, ltp, bid, ask, bq, aq, vol, oi], ...])."""
    p = (folder or (C.DATA / "quotes")) / date / f"{name}.qrec"
    meta, rows = {}, []
    if not p.exists():
        return meta, rows
    data = p.read_bytes()
    off = 0
    while off + 4 <= len(data):
        n = struct.unpack_from("<I", data, off)[0]
        off += 4
        if off + n > len(data):
            break                                       # torn last frame (engine stopped mid-write)
        ch = json.loads(zlib.decompress(data[off:off + n]))
        meta.update(ch["meta"])
        rows.extend(ch["rows"])
        off += n
    rows.sort(key=lambda r: r[0])
    return meta, rows
