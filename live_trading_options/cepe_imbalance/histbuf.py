"""
cepe_imbalance/histbuf.py - today's chart samples, stored compactly.

20-Nov-2026: the engine kept every 0.5 s chart sample of the day as a Python dict - 2.19 million samples over
25 books at ~1.4 KB each = ~3.1 GB of its 4.4 GB. Windows pushed most of it to the pagefile, every chart
request walked 75-156 k dicts per book, and opening a book took 0.5-4 s.

HistBuf holds the same samples in typed columns (8 bytes per number, ~0.18 KB a sample) and behaves like the
old list for every reader: len(), [i], [a:b], iteration, bisect, `is` identity per day. A row read back is a
dict with the same keys and values as the one appended (numbers come back as float; None stays None).
Keys outside the known set, or values of an unexpected type, are kept as-is in a side dict - nothing is lost.

Fast paths for the hot readers: downsample() (chart for the page), index_after() (new samples since t),
extremes() (day high / low without building dicts). Thread-safe: the engine appends while web threads read.
"""
import bisect
import math
import threading
from array import array

FIELDS = ("mtm", "imb", "ce", "pe", "ref", "atm", "st", "ace", "ape")
_BIT = {f: 1 << i for i, f in enumerate(FIELDS)}
_RED_SET, _RED_TRUE, _SP = 1 << 9, 1 << 10, 1 << 11
SP_N = 6
NAN = float("nan")
_KNOWN = set(FIELDS) | {"t", "st_red", "sp"}


def _num(v):
    """float for a number, NaN for None; raises for anything else (-> kept in the side dict)."""
    if v is None:
        return NAN
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError
    return float(v)


def _out(x):
    return None if x != x else x                     # NaN -> None


class HistBuf:
    __slots__ = ("t", "cols", "flags", "sp", "extra", "_off", "_lock", "__weakref__")

    def __init__(self, rows=()):
        self.t = array("d")
        self.cols = {f: array("d") for f in FIELDS}
        self.flags = array("H")
        self.sp = array("d")
        self.extra = {}                              # absolute row number -> {key: value} kept verbatim
        self._off = 0                                # rows trimmed off the front so far
        self._lock = threading.Lock()
        for r in rows:
            self.append(r)

    # ── writing ──────────────────────────────────────────────────────────────
    def append(self, row: dict):
        fl, vals, xtra = 0, {}, {}
        for k, v in row.items():
            if k == "t":
                continue
            if k in _BIT:
                try:
                    vals[k] = _num(v)
                    fl |= _BIT[k]
                    continue
                except TypeError:
                    pass
            elif k == "st_red" and isinstance(v, bool):
                fl |= _RED_SET | (_RED_TRUE if v else 0)
                continue
            elif k == "sp" and isinstance(v, list) and len(v) == SP_N:
                try:
                    spv = [_num(x) for x in v]
                    fl |= _SP
                    vals["sp"] = spv
                    continue
                except TypeError:
                    pass
            xtra[k] = v
        t = float(row["t"])
        with self._lock:
            for f in FIELDS:
                self.cols[f].append(vals.get(f, NAN))
            self.sp.extend(vals.get("sp") or (NAN,) * SP_N)
            self.flags.append(fl)
            if xtra:
                self.extra[self._off + len(self.t)] = xtra
            self.t.append(t)                          # last: a row is visible only when complete

    def trim(self, keep: int, slack: int = 10000):
        """Keep the newest `keep` rows; trims only when `slack` over, so it costs nothing per append."""
        with self._lock:
            n = len(self.t) - keep
            if n <= slack:
                return
            del self.t[:n]
            for c in self.cols.values():
                del c[:n]
            del self.flags[:n]
            del self.sp[:n * SP_N]
            self._off += n
            if self.extra:
                self.extra = {k: v for k, v in self.extra.items() if k >= self._off}

    # ── reading ──────────────────────────────────────────────────────────────
    def _row(self, i: int) -> dict:
        fl = self.flags[i]
        r = {"t": self.t[i]}
        cols = self.cols
        for f in ("mtm", "imb", "ce", "pe", "ref", "atm", "st"):
            if fl & _BIT[f]:
                r[f] = _out(cols[f][i])
        if fl & _RED_SET:
            r["st_red"] = bool(fl & _RED_TRUE)
        for f in ("ace", "ape"):
            if fl & _BIT[f]:
                r[f] = _out(cols[f][i])
        if fl & _SP:
            r["sp"] = [_out(x) for x in self.sp[i * SP_N:(i + 1) * SP_N]]
        x = self.extra.get(self._off + i) if self.extra else None
        if x:
            r.update(x)
        return r

    def __len__(self):
        return len(self.t)

    def __bool__(self):
        return len(self.t) > 0

    def __getitem__(self, k):
        with self._lock:
            n = len(self.t)
            if isinstance(k, slice):
                return [self._row(i) for i in range(*k.indices(n))]
            if k < 0:
                k += n
            if not 0 <= k < n:
                raise IndexError("HistBuf index out of range")
            return self._row(k)

    def __iter__(self):
        return iter(self[:])

    def __delitem__(self, k):
        """Only `del buf[:-keep]` / `del buf[:n]` (front trim), as the old list code did."""
        if not isinstance(k, slice) or k.start is not None or k.step not in (None, 1):
            raise TypeError("HistBuf supports only front trims: del buf[:n]")
        with self._lock:
            n = len(self.t)
            cut = range(*k.indices(n)).stop if k.stop is not None else n
        if cut > 0:
            self.trim(n - cut, slack=0)

    def index_after(self, t: float) -> int:
        """First row with time > t (the old `bisect_right(rows, t, key=time)`)."""
        with self._lock:
            return bisect.bisect_right(self.t, t)

    def since(self, t: float) -> list:
        with self._lock:
            i = bisect.bisect_right(self.t, t)
            return [self._row(j) for j in range(i, len(self.t))]

    def downsample(self, max_points: int) -> list:
        """Same choice of rows as days.downsample(list(rows), max_points), without building every dict."""
        with self._lock:
            t = self.t
            n = len(t)
            if n <= max_points:
                return [self._row(i) for i in range(n)]
            t0, t1 = t[0], t[n - 1]
            width = (t1 - t0) / max_points or 1
            idx, last = [], None
            app = idx.append
            for i, x in enumerate(t.tolist()):            # one C-level copy, then a plain float loop
                b = int((x - t0) / width)
                if b == last:
                    idx[-1] = i
                else:
                    app(i)
                    last = b
            if idx[0] != 0:
                idx.insert(0, 0)
            return [self._row(i) for i in idx]

    def extremes(self, start: int = 0):
        """From row `start`: (mtm max, mtm min, imb max, imb min, last row's time, last row's mtm, rows).
        None for a series with no value. Reads the columns only (no dicts)."""
        with self._lock:
            n = len(self.t)
            if start >= n:
                return None, None, None, None, None, None, 0
            mtm, imb = self.cols["mtm"], self.cols["imb"]
            fl, bm, bi = self.flags, _BIT["mtm"], _BIT["imb"]
            hi = lo = imx = imn = None
            for i in range(start, n):
                f = fl[i]
                if f & bm:
                    m = mtm[i]
                    if m == m:
                        if hi is None or m > hi:
                            hi = m
                        if lo is None or m < lo:
                            lo = m
                if f & bi:
                    v = imb[i]
                    if v == v:
                        if imx is None or v > imx:
                            imx = v
                        if imn is None or v < imn:
                            imn = v
            lm = _out(mtm[n - 1]) if fl[n - 1] & bm else None
            return hi, lo, imx, imn, self.t[n - 1], lm, n - start

    def nbytes(self) -> int:
        return (self.t.itemsize * len(self.t) * (1 + len(FIELDS)) + self.flags.itemsize * len(self.flags)
                + self.sp.itemsize * len(self.sp))
