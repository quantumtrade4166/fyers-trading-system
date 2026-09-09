"""
deployment/btc_api.py — what the BTC Delta Neutral tab reads.
=============================================================

The engine is a SEPARATE process. This module only reads the files it publishes;
it never imports the strategy, never computes anything, and never writes. That
separation is deliberate: the dashboard's event loop must not be able to stall a
book that is holding positions, and a broken dashboard must not be able to take
the paper run down with it.

Two endpoints carry the tab, split by how fast they change:

    /api/btc/tick    tiny, polled ~1s   spot + live P&L + per-leg marks
    /api/btc/state   larger, ~5s        books, chain, feed health, session state

Splitting them is the whole reason the tab can feel live. Re-sending the option
chain every second to move a price would be wasted bandwidth on every refresh,
and making the ticker wait for the chain is what makes a screen feel dead.

Everything degrades to an explicit "stale" rather than an error: if the engine is
down the files simply stop changing, and the tab says so instead of showing a
frozen number as though it were current.
"""

import json
import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/btc", tags=["btc"])

BTC = Path(__file__).resolve().parents[1] / "live_trading_options" / "delta_btc"
STATE = BTC / "data" / "live_state"
RESULTS = BTC / "data" / "results"
LOGS = BTC / "logs"

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PROFILES = ["ist_day", "full_cycle", "continuous"]


def _now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _age_seconds(stamp: str, fmt: str) -> float | None:
    """How old a written timestamp is, in seconds. The tab uses this to decide
    whether to show a value as live or greyed out."""
    try:
        t = dt.datetime.strptime(stamp, fmt)
        if fmt == "%H:%M:%S":
            t = _now_ist().replace(hour=t.hour, minute=t.minute, second=t.second,
                                   microsecond=0)
        return (_now_ist() - t).total_seconds()
    except Exception:
        return None


@router.get("/tick")
def tick():
    """The fast path. Polled about once a second by the ticker."""
    d = _read_json(STATE / "TICK.json")
    if d is None:
        return {"ok": False, "reason": "engine has not written a tick yet",
                "stale": True}
    age = _age_seconds(d.get("ts", ""), "%H:%M:%S")
    # 20s of silence means the engine is gone, not that the market is quiet —
    # Delta pushes spot several times a second.
    d["age"] = round(age, 1) if age is not None else None
    d["stale"] = age is None or age > 20
    d["ok"] = True
    return d


@router.get("/state")
def state():
    """Everything else: the three books, the option chain, session state, and the
    feed's own health."""
    d = _read_json(STATE / "ALL_STATE.json")
    if d is None:
        return {"ok": False, "reason": "engine has not written state yet",
                "stale": True, "profiles": {}}
    age = _age_seconds(d.get("updated", ""), "%Y-%m-%d %H:%M:%S")
    d["age"] = round(age, 1) if age is not None else None
    d["stale"] = age is None or age > 60
    d["ok"] = True
    return d


@router.get("/equity")
def equity(profile: str = Query(None), day: str = Query(None)):
    """Intraday MTM samples — the equity curve.

    Defaults to TODAY for every profile. The engine samples once a minute, so a
    full day is ~1,400 rows per profile; the tab draws them directly.
    """
    day = day or _now_ist().date().isoformat()
    names = [profile] if profile else PROFILES
    out = {}
    for name in names:
        rows = []
        f = RESULTS / f"{name}_equity.jsonl"
        if f.exists():
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or day not in line[:30]:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            except Exception:
                pass
        out[name] = rows
    return {"ok": True, "day": day, "equity": out}


@router.get("/dates")
def dates():
    """Every day that has data, newest first, with a one-line summary each.

    A month-long experiment is only reviewable if you can open any day of it. The
    files were already append-only, so the history was there — this just makes it
    addressable, and reports per-day what is actually in it so the picker can show
    an empty day as empty instead of looking broken.
    """
    days: dict = {}

    def touch(d: str):
        return days.setdefault(d, {"date": d, "cycles": 0, "pnl": 0.0,
                                   "samples": 0, "events": 0, "profiles": []})

    for f in RESULTS.glob("*_equity.jsonl"):
        name = f.name.replace("_equity.jsonl", "")
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                i = line.find('"ts": "')
                if i < 0:
                    continue
                d = line[i + 7:i + 17]
                if len(d) == 10:
                    rec = touch(d)
                    rec["samples"] += 1
                    if name not in rec["profiles"]:
                        rec["profiles"].append(name)
        except Exception:
            continue

    cf = RESULTS / "cycles.jsonl"
    if cf.exists():
        try:
            for line in cf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d = (r.get("first_entry") or r.get("ended") or "")[:10]
                if len(d) == 10:
                    rec = touch(d)
                    rec["cycles"] += 1
                    if not r.get("late_start"):
                        rec["pnl"] = round(rec["pnl"] + r.get("realized", 0), 4)
        except Exception:
            pass

    for f in LOGS.glob("*_btc_audit.log"):
        d = f.name[:10]
        if len(d) == 10:
            try:
                touch(d)["events"] = sum(1 for _ in f.open(encoding="utf-8"))
            except Exception:
                pass

    out = sorted(days.values(), key=lambda x: x["date"], reverse=True)
    for r in out:
        r["profiles"].sort()
    return {"ok": True, "days": out, "today": _now_ist().date().isoformat()}


@router.get("/cycles")
def cycles(limit: int = Query(60, ge=1, le=1000), day: str = Query(None)):
    """Completed cycles, newest first — the month's actual dataset.

    `day` filters to cycles that STARTED that day. A B/C cycle runs 17:35 to 17:10
    the next day, so filtering on the end date would file most of them under the
    day after the one you were watching.
    """
    f = RESULTS / "cycles.jsonl"
    rows = []
    if f.exists():
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
    rows.reverse()
    if day:
        rows = [r for r in rows
                if (r.get("first_entry") or r.get("ended") or "").startswith(day)]

    # Per-profile totals over WHATEVER IS BEING SHOWN — the whole run by default,
    # or one day when `day` is set. Computed here rather than in the browser so the
    # header can never disagree with the table under it, or with compare.py.
    totals = {}
    for name in PROFILES:
        mine = [r for r in rows if r.get("profile") == name]
        # A late-start or interrupted cycle is evidence about the plumbing, not
        # about the strategy. Both are shown, neither is averaged in.
        clean = [r for r in mine
                 if not r.get("late_start") and not r.get("interrupted")]
        totals[name] = {
            "cycles": len(clean),
            "total": round(sum(r.get("realized", 0) for r in clean), 4),
            "fees": round(sum(r.get("fees", 0) for r in clean), 4),
            "wins": sum(1 for r in clean if r.get("realized", 0) > 0),
            "excluded": len(mine) - len(clean),
            "late_start": sum(1 for r in mine if r.get("late_start")),
            "interrupted": sum(1 for r in mine if r.get("interrupted")),
        }
    return {"ok": True, "cycles": rows[:limit], "totals": totals,
            "counted": "excludes late-start and interrupted cycles"}


@router.get("/audit")
def audit(limit: int = Query(80, ge=1, le=500), day: str = Query(None)):
    """Today's event log, newest last — entries, adjustments, stops, square-offs.

    This is the 'why did it do that' view. Reading it beats reconstructing intent
    from a position table, which is the mistake that made the 25-Aug NSE incident
    invisible for four hours.
    """
    day = day or _now_ist().date().isoformat()
    f = LOGS / f"{day}_btc_audit.log"
    rows = []
    if f.exists():
        try:
            for line in f.read_text(encoding="utf-8").splitlines()[-limit:]:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
    return {"ok": True, "day": day, "events": rows}
