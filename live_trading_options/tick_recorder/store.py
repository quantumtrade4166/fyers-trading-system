"""
tick_recorder/store.py — tick journal (live) + compact 15-minute column files (kept) + the reader.
===================================================================================================

Files (per index, per 15-minute IST slot):
  data/ticks/{date}/{INDEX}_{HHMM}.trec.tmp   live journal, written every 2 s. "*.tmp" is skipped by the
                                              Drive backup, so the bulky journal never reaches Drive.
  data/ticks/{date}/{INDEX}_{HHMM}.tcol       the kept file: written once when the slot is over (~4 bytes
                                              a tick vs ~15 in the journal), then the journal is deleted.
Why: the Drive mirror is copy-only (a file deleted on the VPS stays on Drive), so compacting AFTER a
backup would leave both copies there forever; and a growing file is re-uploaded every 10 minutes.
Owner 13-Nov-2026: ~1,250 ticks/s for NIFTY+SENSEX -> ~450 MB/day raw, ~120 MB/day compact.

Packing runs in a SEPARATE below-normal-priority process (python -m ...tick_recorder.store <journal>):
in the recorder's own process it held the GIL for 10+ s and stalled the feed reader.

Journal = frames [uint32 len][zlib(frame)], frame = [uint32 json_len][json {"t0", "new": [[idx, key,
          symbol, kind, expiry, strike]...]}][ROW rows]   (idx per file)
tcol    = b"TCOL1" + lzma( [uint32 json_len][json {"t0", "n", "contracts": [[key, symbol, kind, expiry,
          strike, count]...]}] + one array per column ). Rows are grouped per contract (in "contracts"
          order) and every column is delta-coded within its contract.
Prices are exact: paise as integers (NSE/BSE ticks are 0.05), -1 = none; quantities -1 = none.

read_day(date, index, root) -> (contracts {key: {...}}, rows [(t, key, ltp, bid, ask, bq, aq, vol, oi,
exch_ts)]) sorted by time; read_day_arrays() gives the same as numpy arrays (fast, for backtests).
"""
import collections
import datetime as dt
import json
import logging
import lzma
import os
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import numpy as np

log = logging.getLogger("ticks.store")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROW = struct.Struct("<IHiiiIIIII")
ROW_DT = np.dtype([("dms", "<u4"), ("i", "<u2"), ("ltp", "<i4"), ("bid", "<i4"), ("ask", "<i4"),
                   ("bq", "<u4"), ("aq", "<u4"), ("vol", "<u4"), ("oi", "<u4"), ("ets", "<u4")])
assert ROW_DT.itemsize == ROW.size
NONE_U = 0xFFFFFFFF
FLUSH_S = 2.0
SLOT_MIN = 15
GRACE_S = 45                     # a slot is packed this long after it ends (late ticks still land)
MAGIC = b"TCOL1"
FIELDS = ("ltp", "bid", "ask", "bq", "aq", "vol", "oi", "ets")
PRICE = ("ltp", "bid", "ask")
JOURNAL, PACKING = ".trec.tmp", ".packing.tmp"


def _px(v):
    return int(round(v * 100)) if v is not None and v > 0 else -1


def _u(v):
    return int(v) if v is not None and 0 <= v < NONE_U else NONE_U


def slot_of(t: float):
    d = dt.datetime.fromtimestamp(t, IST)
    m = d.minute - d.minute % SLOT_MIN
    return d.date().isoformat(), f"{d.hour:02d}{m:02d}"


# ── journal ─────────────────────────────────────────────────────────────────
def iter_frames(p: Path):
    data = Path(p).read_bytes()
    off = 0
    while off + 4 <= len(data):
        n = struct.unpack_from("<I", data, off)[0]
        off += 4
        if off + n > len(data):
            break                                        # torn last frame (killed mid-write)
        try:
            b = zlib.decompress(data[off:off + n])
        except zlib.error:
            break
        off += n
        hl = struct.unpack_from("<I", b, 0)[0]
        yield json.loads(b[4:4 + hl]), b[4 + hl:]


def journal_arrays(p: Path):
    """-> (contracts {key: info}, keys [key...], cols {"t": int64 epoch ms, "k": int32 index into keys,
    field: int64}) with none = -1 for every field."""
    contracts, keys, kpos, by_i = {}, [], {}, {}
    ts, ks, parts = [], [], []
    for hdr, blob in iter_frames(p):
        for i, key, sym, kind, exp, strike in hdr["new"]:
            if key not in kpos:
                kpos[key] = len(keys)
                keys.append(key)
            by_i[i] = kpos[key]
            contracts[key] = {"symbol": sym, "kind": kind, "expiry": exp, "strike": strike}
        a = np.frombuffer(blob, dtype=ROW_DT)
        if not len(a):
            continue
        lut = np.zeros(max(by_i) + 1, dtype=np.int32)
        for i, g in by_i.items():
            lut[i] = g
        ts.append(a["dms"].astype(np.int64) + hdr["t0"])
        ks.append(lut[a["i"]])
        parts.append(a)
    if not parts:
        return contracts, keys, {"t": np.zeros(0, np.int64), "k": np.zeros(0, np.int32),
                                 **{f: np.zeros(0, np.int64) for f in FIELDS}}
    a = np.concatenate(parts)
    cols = {"t": np.concatenate(ts), "k": np.concatenate(ks)}
    for f in FIELDS:
        v = a[f].astype(np.int64)
        if f not in PRICE:
            v[a[f] == NONE_U] = -1
        cols[f] = v
    return contracts, keys, cols


# ── compact column file ─────────────────────────────────────────────────────
def write_tcol(dst: Path, contracts: dict, keys: list, cols: dict):
    n = len(cols["t"])
    order = np.lexsort((cols["t"], cols["k"]))           # group per contract, time order inside
    k = cols["k"][order]
    counts = np.bincount(k, minlength=len(keys))
    used = [j for j in range(len(keys)) if counts[j]]
    starts = np.r_[0, np.flatnonzero(k[1:] != k[:-1]) + 1] if n else np.zeros(0, np.int64)
    t0 = int(cols["t"].min()) if n else 0
    parts = []
    for name in ("t",) + FIELDS:
        v = cols[name][order] - (t0 if name == "t" else 0)
        d = np.diff(v, prepend=v[:1])
        d[starts] = v[starts]                             # each contract starts from its raw value
        typ = np.int32 if name in ("t",) + PRICE else np.int64
        parts.append(d.astype(typ).tobytes())
    meta = {"t0": t0, "n": n, "contracts": [[keys[j], contracts[keys[j]]["symbol"], contracts[keys[j]]["kind"],
                                             contracts[keys[j]]["expiry"], contracts[keys[j]]["strike"],
                                             int(counts[j])] for j in used]}
    h = json.dumps(meta, separators=(",", ":")).encode()
    blob = MAGIC + lzma.compress(struct.pack("<I", len(h)) + h + b"".join(parts), preset=6)
    part = dst.with_name(dst.name + ".tmp")
    part.write_bytes(blob)
    os.replace(part, dst)


def tcol_arrays(p: Path):
    """-> (contracts, keys, cols) like journal_arrays (rows grouped per contract)."""
    b = Path(p).read_bytes()
    if b[:5] != MAGIC:
        raise ValueError(f"not a tcol file: {p}")
    b = lzma.decompress(b[5:])
    hl = struct.unpack_from("<I", b, 0)[0]
    meta = json.loads(b[4:4 + hl])
    n, off = meta["n"], 4 + hl
    counts = np.array([c[5] for c in meta["contracts"]], dtype=np.int64)
    starts = np.r_[0, np.cumsum(counts)[:-1]] if len(counts) else np.zeros(0, np.int64)
    cols = {}
    for name in ("t",) + FIELDS:
        typ = np.int32 if name in ("t",) + PRICE else np.int64
        size = np.dtype(typ).itemsize * n
        d = np.frombuffer(b[off:off + size], dtype=typ).astype(np.int64)
        off += size
        cs = np.cumsum(d)                                 # undo the per-contract delta in one pass
        if n:
            base = np.repeat(cs[starts] - d[starts], counts)
            cs = cs - base
        cols[name] = cs + (meta["t0"] if name == "t" else 0)
    keys = [c[0] for c in meta["contracts"]]
    cols["k"] = np.repeat(np.arange(len(keys), dtype=np.int32), counts)
    contracts = {c[0]: {"symbol": c[1], "kind": c[2], "expiry": c[3], "strike": c[4]} for c in meta["contracts"]}
    return contracts, keys, cols


def _merge(a, b):
    """Two (contracts, keys, cols) sets -> one."""
    c1, k1, x1 = a
    c2, k2, x2 = b
    keys = list(dict.fromkeys(k1 + k2))
    pos = {k: i for i, k in enumerate(keys)}
    remap1 = np.array([pos[k] for k in k1], dtype=np.int32) if k1 else np.zeros(0, np.int32)
    remap2 = np.array([pos[k] for k in k2], dtype=np.int32) if k2 else np.zeros(0, np.int32)
    cols = {f: np.concatenate([x1[f], x2[f]]) for f in x1 if f != "k"}
    cols["k"] = np.concatenate([remap1[x1["k"]] if len(x1["k"]) else x1["k"],
                                remap2[x2["k"]] if len(x2["k"]) else x2["k"]]).astype(np.int32)
    return {**c1, **c2}, keys, cols


def _canon(keys, cols):
    """Order-independent view for verification: rows as a sorted structured array with key names."""
    names = np.array(keys, dtype=object)[cols["k"]] if len(cols["k"]) else np.zeros(0, object)
    order = np.lexsort([cols[f] for f in reversed(("t",) + FIELDS)] + [names.astype(str)]) if len(names) else []
    return [names[order].tolist()] + [cols[f][order].tolist() for f in ("t",) + FIELDS]


def pack(journal: Path) -> Path:
    """Packing journal (*.packing.tmp) -> .tcol (merged with an existing .tcol of the same slot), verified,
    then the journal is deleted. Runs in its own low-priority process (see TickStore.compact_due)."""
    name = journal.name
    stem = name[:-len(PACKING)] if name.endswith(PACKING) else name[:-len(JOURNAL)]
    dst = journal.with_name(stem + ".tcol")
    data = journal_arrays(journal)
    if dst.exists():                                      # very late ticks for an already packed slot
        data = _merge(tcol_arrays(dst), data)
    contracts, keys, cols = data
    if len(cols["t"]):
        write_tcol(dst, contracts, keys, cols)
        back = tcol_arrays(dst)
        if _canon(back[1], back[2]) != _canon(keys, cols):
            raise RuntimeError(f"tcol verify failed for {dst.name}; journal kept")
    journal.unlink()
    return dst


# ── reader ──────────────────────────────────────────────────────────────────
def read_day_arrays(date: str, index: str, root: Path):
    """All of one index's day as arrays sorted by time: (contracts, keys, cols); prices in paise."""
    d = Path(root) / date
    acc = None
    files = sorted(d.glob(f"{index}_*.tcol")), sorted(d.glob(f"{index}_*{JOURNAL}")) + sorted(d.glob(f"{index}_*{PACKING}"))
    for p in files[0]:
        acc = tcol_arrays(p) if acc is None else _merge(acc, tcol_arrays(p))
    for p in files[1]:
        acc = journal_arrays(p) if acc is None else _merge(acc, journal_arrays(p))
    if acc is None:
        return {}, [], {f: np.zeros(0, np.int64) for f in ("t", "k") + FIELDS}
    contracts, keys, cols = acc
    o = np.argsort(cols["t"], kind="stable")
    return contracts, keys, {f: v[o] for f, v in cols.items()}


def read_day(date: str, index: str, root: Path):
    contracts, keys, c = read_day_arrays(date, index, root)
    P = lambda v: v / 100 if v >= 0 else None
    Q = lambda v: None if v < 0 else int(v)
    rows = [(int(c["t"][i]) / 1000, keys[c["k"][i]], P(int(c["ltp"][i])), P(int(c["bid"][i])), P(int(c["ask"][i])),
             Q(c["bq"][i]), Q(c["aq"][i]), Q(c["vol"][i]), Q(c["oi"][i]), Q(c["ets"][i])) for i in range(len(c["t"]))]
    return contracts, rows


# ── writer ──────────────────────────────────────────────────────────────────
class TickStore:
    def __init__(self, root: Path, contract_info, start=True, packer=None):
        """contract_info(key) -> (symbol, kind, expiry, strike). packer(path) packs one journal
        (default: a separate below-normal-priority process; tests pass `pack` to run in-process)."""
        self.root = Path(root)
        self.info = contract_info
        self.packer = packer or pack_in_subprocess
        self.q = collections.deque()
        self.idx: dict[Path, dict] = {}                  # journal -> {key: idx}
        self.stats = {"rows": 0, "frames": 0, "journal_bytes": 0, "dropped_frames": 0, "pending": 0,
                      "packed": 0, "tcol_bytes": 0, "pack_errors": 0, "last_pack_s": 0.0}
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        if start:
            threading.Thread(target=self._loop, daemon=True, name="TickStore").start()

    def add(self, index: str, tick):
        self.q.append((index, tick))                     # hot path: one append per tick

    def journal_for(self, index: str, t: float) -> Path:
        date, hhmm = slot_of(t)
        return self.root / date / f"{index}_{hhmm}{JOURNAL}"

    def flush(self):
        with self._wlock:
            n = len(self.q)
            groups: dict[Path, list] = {}
            for _ in range(n):
                index, tk = self.q.popleft()
                groups.setdefault(self.journal_for(index, tk.recv_ts), []).append(tk)
            for p, ticks in groups.items():
                self._write(p, ticks)
            self.stats["pending"] = len(self.q)

    def _write(self, p: Path, ticks):
        known = self.idx.get(p)
        if known is None or not p.exists():
            known = self.idx[p] = self._load_index(p)
        t0 = int(ticks[0].recv_ts * 1000)
        new, rows, added = [], [], []
        for tk in ticks:
            i = known.get(tk.key)
            if i is None:
                i = known[tk.key] = len(known)
                added.append(tk.key)
                sym, kind, exp, strike = self.info(tk.key)
                new.append([i, tk.key, sym, kind, exp, strike])
            rows.append(ROW.pack(max(0, int(tk.recv_ts * 1000) - t0), i, _px(tk.ltp), _px(tk.bid), _px(tk.ask),
                                 _u(tk.bid_qty), _u(tk.ask_qty), _u(tk.volume), _u(tk.oi), _u(tk.exch_ts)))
        hdr = json.dumps({"t0": t0, "new": new}, separators=(",", ":")).encode()
        z = zlib.compress(struct.pack("<I", len(hdr)) + hdr + b"".join(rows), 1)
        if self._append(p, struct.pack("<I", len(z)) + z):
            self.stats["rows"] += len(rows)
        else:
            for k in added:
                known.pop(k, None)

    def _load_index(self, p: Path) -> dict:
        out = {}
        if p.exists():
            for hdr, _ in iter_frames(p):
                for i, key, *_ in hdr["new"]:
                    out[key] = i
        return out

    def _append(self, p: Path, data: bytes) -> bool:
        for _ in range(60):
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "ab") as f:
                    f.write(data)
                self.stats["frames"] += 1
                self.stats["journal_bytes"] += len(data)
                return True
            except PermissionError:
                time.sleep(0.05)
            except Exception:
                log.exception("tick write %s", p.name)
                break
        self.stats["dropped_frames"] += 1
        log.warning("tick frame dropped for %s", p.name)
        return False

    def compact_due(self, now: float | None = None, all_=False):
        """Pack every journal whose slot ended GRACE_S ago (all_: every journal), plus any packing file a
        crash left behind. The journal is renamed under the write lock (instant), then packed outside it:
        a late tick for that slot just starts a new journal, merged on its own pack."""
        now = now or time.time()
        for j in sorted(self.root.glob(f"*/*{JOURNAL}")):
            date, stem = j.parent.name, j.name[:-len(JOURNAL)]
            hhmm = stem.rsplit("_", 1)[-1]
            try:
                start = dt.datetime.fromisoformat(f"{date}T{hhmm[:2]}:{hhmm[2:]}:00+05:30").timestamp()
            except ValueError:
                continue
            if not all_ and now < start + SLOT_MIN * 60 + GRACE_S:
                continue
            with self._wlock:
                if j.with_name(stem + PACKING).exists():
                    continue                              # previous pack of this slot not done: next pass
                os.replace(j, j.with_name(stem + PACKING))
                self.idx.pop(j, None)
        for pk in sorted(self.root.glob(f"*/*{PACKING}")):
            t = time.time()
            try:
                out = self.packer(pk)
                self.stats["packed"] += 1
                self.stats["last_pack_s"] = round(time.time() - t, 1)
                if out and Path(out).exists():
                    self.stats["tcol_bytes"] += Path(out).stat().st_size
                log.info("packed %s/%s in %.1f s", pk.parent.name, pk.name, time.time() - t)
            except Exception:
                self.stats["pack_errors"] += 1
                log.exception("pack %s", pk.name)

    def _loop(self):
        last_c = 0.0
        while not self._stop.wait(FLUSH_S):
            try:
                self.flush()
            except Exception:
                log.exception("tick flush")
            if time.time() - last_c > 20 and not getattr(self, "_packing", False):
                last_c = time.time()                      # packing waits on a subprocess: own thread
                self._packing = True
                threading.Thread(target=self._safe_compact, daemon=True, name="TickPack").start()

    def _safe_compact(self):
        try:
            self.compact_due()
        except Exception:
            log.exception("pack pass")
        finally:
            self._packing = False

    def stop(self):
        self._stop.set()
        self.flush()
        while getattr(self, "_packing", False):
            time.sleep(0.5)
        self.compact_due(all_=True)


BELOW_NORMAL = 0x00004000


def pack_in_subprocess(p: Path):
    """Pack one journal in a separate, below-normal-priority Python process (no GIL contention with the
    feed reader; never competes with the trading engines for CPU)."""
    flags = BELOW_NORMAL | getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    r = subprocess.run([sys.executable, "-m", "live_trading_options.tick_recorder.store", str(p)],
                       capture_output=True, text=True, timeout=900, creationflags=flags,
                       cwd=str(Path(__file__).resolve().parents[2]))
    if r.returncode != 0:
        raise RuntimeError(f"packer exit {r.returncode}: {(r.stderr or r.stdout)[-400:]}")
    return Path(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None


if __name__ == "__main__":                                # packer process: pack one journal, print the result
    print(pack(Path(sys.argv[1])))
