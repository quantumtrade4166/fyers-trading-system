"""
cepe_imbalance/days.py — read back past trading days from the saved paper files (read-only).
==========================================================================================

Per day `data/books/{date}/` holds {name}.json (positions incl. realised P&L), {name}_trades.jsonl,
{name}_events.jsonl, and `data/history/{date}/{name}.jsonl` the 0.5 s MTM / imbalance samples.
EOD P&L of a finished day = realised + any still-open lots valued at the last sample (normally 0:
everything is squared off at 15:15 / 23:45).
"""
import json
import os

from live_trading_options.cepe_imbalance import config as C

_cache: dict = {}            # path -> (mtime, size, rows)


def jsonl(path) -> list:
    """Parsed JSON-lines file, cached until the file changes."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    hit = _cache.get(str(path))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass                                  # a half-written last line
    _cache[str(path)] = (st.st_mtime, st.st_size, rows)
    return rows


def list_days() -> list:
    """Dates (newest first) that have any saved book or history."""
    out = set()
    for sub in ("books", "history"):
        d = C.DATA / sub
        if d.exists():
            out |= {p.name for p in d.iterdir() if p.is_dir() and any(p.iterdir())}
    return sorted(out, reverse=True)


def downsample(rows: list, max_points: int = 4000) -> list:
    """Keep at most ~max_points rows: one per time bucket (the last one), plus the very first and last."""
    if len(rows) <= max_points:
        return rows
    t0, t1 = rows[0]["t"], rows[-1]["t"]
    width = (t1 - t0) / max_points or 1
    out, last_bucket = [], None
    for r in rows:
        b = int((r["t"] - t0) / width)
        if b == last_bucket:
            out[-1] = r
        else:
            out.append(r)
            last_bucket = b
    if out[0] is not rows[0]:
        out.insert(0, rows[0])
    return out


def history(date: str, name: str) -> list:
    return jsonl(C.DATA / "history" / date / f"{name}.jsonl")


DS_POINTS = 8000                     # what the page asks for (max_points=8000)


def hist_stats(date: str, name: str):
    """Small per-day digest of a finished day's history: MTM/imbalance extremes, last MTM and an 8000-point
    chart copy. Cached on disk next to the history (rebuilt if the history file changes).
    13-Nov: the past-day table re-parsed every full 0.5 s history (78k lines for GOLD) on every request;
    right after a restart that took longer than the dashboard proxy's 15 s, so past days showed nothing."""
    src = C.DATA / "history" / date / f"{name}.jsonl"
    try:
        st = os.stat(src)
    except OSError:
        return None
    cp = src.with_name(f"{name}.stats.json")
    hit = _cache.get(str(cp))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        doc = json.loads(cp.read_text(encoding="utf-8"))
        if doc.get("src_size") == st.st_size and doc.get("src_mtime") == st.st_mtime:
            _cache[str(cp)] = (st.st_mtime, st.st_size, doc)
            return doc
    except (OSError, ValueError):
        pass
    h = jsonl(src)
    mtms = [x["mtm"] for x in h if x.get("mtm") is not None]
    imbs = [x["imb"] for x in h if x.get("imb") is not None]
    doc = {"src_size": st.st_size, "src_mtime": st.st_mtime, "n": len(h),
           "mtm_high": max(mtms) if mtms else None, "mtm_low": min(mtms) if mtms else None,
           "imb_max": max(imbs) if imbs else None, "imb_min": min(imbs) if imbs else None,
           "last_mtm": h[-1].get("mtm") if h else None, "ds": downsample(h, DS_POINTS)}
    from live_trading_options.cepe_imbalance.book import atomic_write
    atomic_write(cp, json.dumps(doc, separators=(",", ":")))
    _cache.pop(str(src), None)                         # the full rows are not needed in memory any more
    _cache[str(cp)] = (st.st_mtime, st.st_size, doc)
    return doc


def warm(names, today: str):
    """Build the digests of every past day once (engine start, background thread)."""
    for d in list_days():
        if d == today:
            continue
        for n in names:
            try:
                hist_stats(d, n)
            except Exception:
                pass


def trades(date: str, name: str) -> list:
    return jsonl(C.DATA / "books" / date / f"{name}_trades.jsonl")


def events(date: str, name: str) -> list:
    return jsonl(C.DATA / "books" / date / f"{name}_events.jsonl")


def positions(date: str, name: str) -> list:
    p = C.DATA / "books" / date / f"{name}.json"
    if not p.exists():
        return []
    doc = json.loads(p.read_text(encoding="utf-8"))
    return sorted(doc.get("positions", {}).values(), key=lambda x: (x["expiry"], x["kind"], x["strike"]))


def day_pnl(date: str, name: str):
    """EOD P&L of one saved day, or None if the instrument never traded that day. Reads only the small
    book + trades files; the big history file only when lots were left open (rare)."""
    if not trades(date, name):
        return None
    pos = positions(date, name)
    realized = sum(p.get("realized", 0.0) for p in pos)
    if any(p.get("lots") for p in pos):
        s = hist_stats(date, name)
        if s and s.get("last_mtm") is not None:
            return round(s["last_mtm"], 2)
    return round(realized, 2)


def pnl_series(name: str, today: str = "", today_pnl=None) -> list:
    """Every traded day oldest first: date, day P&L, cumulative P&L, drawdown from the running peak."""
    out, cum, peak = [], 0.0, 0.0
    dates = sorted(set(list_days()) | ({today} if today else set()))
    for d in dates:
        p = today_pnl if (d == today and today_pnl is not None) else (None if d == today else day_pnl(d, name))
        if p is None:
            continue
        cum += p
        peak = max(peak, cum)
        out.append({"date": d, "pnl": round(p, 2), "cum": round(cum, 2), "dd": round(cum - peak, 2)})
    return out


def summary(date: str, name: str, hist=None) -> dict:
    """EOD numbers for one instrument on one saved day. `hist`: today's in-memory samples (never
    re-parse today's growing history file: 7 x ~6 MB every 3 s pegged the CPU on 4-Nov)."""
    pos = positions(date, name)
    tr = trades(date, name)
    ev = events(date, name)
    realized = sum(p.get("realized", 0.0) for p in pos)
    open_lots = sum(abs(p.get("lots", 0)) for p in pos)
    if hist is None:                                   # past day: the cached digest, never the full file
        s = hist_stats(date, name) or {}
        last_mtm = s.get("last_mtm") if s.get("last_mtm") is not None else realized
        hi, lo, imx, imn = s.get("mtm_high"), s.get("mtm_low"), s.get("imb_max"), s.get("imb_min")
    else:
        h = hist
        last_mtm = h[-1]["mtm"] if h else realized
        mtms = [x["mtm"] for x in h if x.get("mtm") is not None]
        imbs = [x["imb"] for x in h if x.get("imb") is not None]
        hi, lo = (max(mtms), min(mtms)) if mtms else (None, None)
        imx, imn = (max(imbs), min(imbs)) if imbs else (None, None)
    adj = [e for e in ev if e.get("event") == "adjust" and e.get("action") != "entry"
           and not str(e.get("action", "")).startswith("entry")]
    return {
        "name": name, "label": C.label_of(name),
        "pnl": round(realized if not open_lots else last_mtm, 2),
        "realized": round(realized, 2), "open_lots": open_lots,
        "fills": len(tr), "adjustments": len(adj),
        "atm_rule": sum(1 for e in adj if str(e.get("action", "")).startswith(("ATM rule", "roll "))),
        "a": sum(1 for e in adj if str(e.get("action", "")).startswith("A ")),
        "b": sum(1 for e in adj if str(e.get("action", "")).startswith("B ")),
        "mtm_high": hi, "mtm_low": lo, "imb_max": imx, "imb_min": imn,
        "first": tr[0]["time"][:8] if tr else None, "last": tr[-1]["time"][:8] if tr else None,
        "traded": bool(tr),
    }
