"""
cepe_imbalance/st_sim.py — SIMULATED Supertrend-variant MTM for days before the variant existed.
================================================================================================

Owner 14-Nov-2026: "of the past days in the supertrend version, make up the MTM chart from the normal
version's chart so I can visually see". The variant went live 13-Nov; 2–12 Nov only have the normal book.

How (an approximation, labelled "simulated" on the page):
  * Supertrend(10, 3) on CLOSED 1-min candles of the normal book's ATM CE+PE (history ace + ape), seeded
    with the previous saved day's last 150 min when that day traded the SAME expiry (same rule as live).
  * In the trade while the line is red, flat while green. Opening rule as live: red since the open ->
    enter only once CE+PE trades below the first minute's low (or after the first flip).
  * While in, the simulated MTM moves exactly like the normal book's MTM; while flat it stays put.
Not modelled: re-entering re-centres the strikes (the real variant opens fresh ATM legs), quote-width
waits, spreads/costs of each exit + re-entry, the variant's own MTM stop.
"""
import datetime as dt
import json
import os

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import days
from live_trading_options.cepe_imbalance.st_signal import supertrend

VERSION = 3


def _bars(rows, t_from, t_to, tf=60):
    out = []
    for r in rows:
        t = r.get("t", 0)
        if not (t_from <= t < t_to) or r.get("ace") is None or r.get("ape") is None:
            continue
        v, m = r["ace"] + r["ape"], int(t) // tf * tf
        b = out[-1] if out else None
        if b and b["t"] == m:
            b["h"], b["l"], b["c"] = max(b["h"], v), min(b["l"], v), v
        else:
            out.append({"t": m, "o": v, "h": v, "l": v, "c": v})
    return out


def _expiry(date, und):
    tr = days.trades(date, und)
    return tr[0].get("expiry") if tr else None


def simulate(date: str, und: str, per: int = 10, mul: float = 3.0, tf: int = 60, reverse: bool = False):
    """-> {"rows": [...history-like rows...], "pnl": final simulated P&L, "warm_day": d|None} or None.
    tf = candle seconds for the Supertrend (60 = live rule; others only for what-if tests).
    reverse = what-if mirror: in the trade while GREEN, out while red; "green since the open" waits for CE+PE
    to break ABOVE the first minute's high."""
    from live_trading_options.cepe_imbalance.engine import session
    rows = days.history(date, und)
    if not rows:
        return None
    o, close, sq = session(und, dt.date.fromisoformat(date))
    o_ts, c_ts, sq_ts = o.timestamp(), close.timestamp(), sq.timestamp()
    exp = _expiry(date, und)

    warm, warm_day = [], None
    prev = [d for d in days.list_days() if d < date and (C.DATA / "history" / d / f"{und}.jsonl").exists()]
    if prev and exp and _expiry(prev[0], und) == exp:
        po, pc, _ = session(und, dt.date.fromisoformat(prev[0]))
        t_to = pc.timestamp()
        pb = _bars(days.history(prev[0], und), max(po.timestamp(), t_to - C.ST_WARMUP_MIN * 60 * tf // 60), t_to, tf)
        warm, warm_day = pb[:-1] if pb and pb[-1]["t"] + tf > t_to else pb, prev[0]

    today = _bars(rows, o_ts, c_ts, tf)
    first_min = _bars(rows, o_ts, c_ts)[:1]
    series = supertrend(warm + today, per, mul)
    by_t = {t: (v, up) for t, v, up in series}
    start_up = by_t[warm[-1]["t"]][1] if warm and warm[-1]["t"] in by_t else None
    # Opening rule's "first candle": live = the market's first minute. The normal book's history only starts
    # when that book started (09:20 for NIFTY/SENSEX), so the first RECORDED minute stands in for it.
    # 14-Nov fix: this used to require the 09:15 candle, which index days never have -> "red since the open"
    # could never be entered and the curve stayed flat until the first green->red flip (9-Nov SENSEX 13:11).
    first_low = first_min[0]["l"] if first_min else None
    first_high = first_min[0]["h"] if first_min else None
    # Supertrend as known at time T = the reading of the last candle that CLOSED by T
    known = [(b["t"] + tf, by_t[b["t"]]) for b in today if b["t"] in by_t]
    if not known:
        return None                                    # ATM prices were not recorded that day

    out, k, cur = [], 0, None
    holding, entry_mtm, banked, flipped = False, 0.0, 0.0, False
    for r in rows:
        t = r.get("t", 0)
        while k < len(known) and known[k][0] <= t:
            nv = known[k][1]
            if start_up is None:
                start_up = nv[1]                       # no warm-up: the first reading of the day
            elif nv[1] != start_up:
                flipped = True
            cur = nv
            k += 1
        m = r.get("mtm")
        if m is None:
            continue
        if t >= sq_ts:                                 # square-off time: out for the day, no new entries
            if holding:
                banked += m - entry_mtm
                holding = False
        elif o_ts <= t and cur is not None:
            red = cur[1] if reverse else not cur[1]             # red = "in-trade colour"
            if holding and not red:
                banked += m - entry_mtm
                holding = False
            elif not holding and red:
                px = (r["ace"] + r["ape"]) if r.get("ace") is not None and r.get("ape") is not None else None
                gated = (start_up is reverse) and not flipped
                brk = (px is not None) and ((first_high is not None and px > first_high) if reverse
                                            else (first_low is not None and px < first_low))
                if not gated or brk:
                    holding, entry_mtm = True, m
        sim = banked + (m - entry_mtm if holding else 0.0)
        x = {k2: r[k2] for k2 in ("t", "ref", "atm", "ace", "ape", "sp") if k2 in r}
        x["mtm"] = round(sim, 2)
        x["imb"] = r.get("imb") if holding else None
        if cur is not None:
            x["st"], x["st_red"] = cur[0], not cur[1]
        x["in"] = holding
        out.append(x)
    return {"rows": out, "pnl": out[-1]["mtm"] if out else 0.0, "warm_day": warm_day, "expiry": exp}


def sim_digest(date: str, name: str):
    """Cached 8000-point simulated history for a variant book on a day it did not run (None otherwise)."""
    if not C.is_variant(name):
        return None
    hdir = C.DATA / "history" / date
    if (hdir / f"{name}.jsonl").exists():
        return None                                    # the real variant ran that day
    und = C.und_of(name)
    src = hdir / f"{und}.jsonl"
    try:
        st = os.stat(src)
    except OSError:
        return None
    cp = hdir / f"{name}.sim.json"
    stamp = [VERSION, st.st_size, st.st_mtime]
    hit = days._cache.get(str(cp))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        doc = json.loads(cp.read_text(encoding="utf-8"))
        if doc.get("stamp") == stamp:
            days._cache[str(cp)] = (stamp, doc)
            return doc
    except (OSError, ValueError):
        pass
    res = simulate(date, und)
    if res is None:
        return None
    doc = {"stamp": stamp, "pnl": res["pnl"], "warm_day": res["warm_day"], "expiry": res["expiry"],
           "ds": days.downsample(res["rows"], days.DS_POINTS)}
    from live_trading_options.cepe_imbalance.book import atomic_write
    atomic_write(cp, json.dumps(doc, separators=(",", ":")))
    days._cache.pop(str(src), None)
    days._cache[str(cp)] = (stamp, doc)
    return doc
