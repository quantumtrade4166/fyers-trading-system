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


@router.get("/cycles")
def cycles(limit: int = Query(60, ge=1, le=1000)):
    """Completed cycles, newest first — the month's actual dataset."""
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

    # per-profile totals across EVERY cycle, so the tab's header does not have to
    # re-derive them and cannot disagree with the comparison tool
    totals = {}
    for name in PROFILES:
        mine = [r for r in rows if r.get("profile") == name]
        clean = [r for r in mine if not r.get("late_start")]
        totals[name] = {
            "cycles": len(clean),
            "total": round(sum(r.get("realized", 0) for r in clean), 4),
            "fees": round(sum(r.get("fees", 0) for r in clean), 4),
            "wins": sum(1 for r in clean if r.get("realized", 0) > 0),
            "late_start": len(mine) - len(clean),
        }
    return {"ok": True, "cycles": rows[:limit], "totals": totals,
            "counted": "excludes late-start cycles"}


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
