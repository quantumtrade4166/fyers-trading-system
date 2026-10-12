"""
deployment/stdir_api.py — what the Nifty ST Directional tab reads.
=================================================================

The engine (live_trading_options/nifty_st_directional/engine.py) is a SEPARATE
process. This module only reads the files it publishes: it never imports the
strategy and never writes, so a broken dashboard cannot touch the paper book.

    /api/stdir/tick    ~1 s   spot, bias, open position, live MTM, equity
    /api/stdir/state   ~3 s   spot + option candles, Supertrend, reference low, trades, log
    /api/stdir/mtm     MTM samples for a day (5 s) -> MTM chart
    /api/stdir/daily   per-day summary -> equity curve + days table
    /api/stdir/dates   days that have a chart

Staleness is explicit: a stopped engine stops rewriting its files and the page
says STALE instead of showing a frozen number as live.
"""
import json
import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/stdir", tags=["nifty-st-directional"])

BASE = Path(__file__).resolve().parents[1] / "live_trading_options" / "nifty_st_directional"
STATE = BASE / "data" / "live_state"
RESULTS = BASE / "data" / "results"
PARAMS = BASE / "config" / "parameters.json"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _now() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


@router.get("/tick")
def tick():
    d = _read(STATE / "TICK.json")
    if d is None:
        return {"ok": False, "stale": True, "reason": "engine has not published yet"}
    if d.get("date") != _now().date().isoformat():
        d.update(ok=True, stale=True, age=None, reason=f"last run was {d.get('date')}")
        return d
    try:
        t = dt.datetime.strptime(d.get("ts", ""), "%H:%M:%S")
        n = _now()
        age = (n - n.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)).total_seconds()
    except Exception:
        age = None
    d["age"] = round(age, 1) if age is not None else None
    d["stale"] = age is None or age > 15
    d["ok"] = True
    return d


@router.get("/state")
def state(date: str = Query(None)):
    date = date or _now().date().isoformat()
    d = _read(STATE / f"{date}_STATE.json")
    if d is None:
        return {"ok": False, "date": date, "reason": f"no chart for {date}"}
    try:
        upd = dt.datetime.strptime(d.get("updated", ""), "%Y-%m-%d %H:%M:%S")
        d["age"] = round((_now() - upd).total_seconds(), 1)
    except Exception:
        d["age"] = None
    d["live_day"] = date == _now().date().isoformat()
    d["stale"] = d["live_day"] and (d["age"] is None or d["age"] > 30)
    d["ok"] = True
    return d


@router.get("/mtm")
def mtm(date: str = Query(None)):
    date = date or _now().date().isoformat()
    f = RESULTS / f"{date}_mtm.jsonl"
    rows = []
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return {"ok": True, "date": date, "rows": rows}


@router.get("/daily")
def daily():
    days = _read(RESULTS / "daily.json", {}) or {}
    capital = (_read(PARAMS, {}) or {}).get("capital", 1_000_000)
    rows = sorted(days.values(), key=lambda r: r["date"])
    equity, peak, dd_max, curve = capital, capital, 0.0, []
    for r in rows:
        equity = r.get("equity_close", equity + r.get("net", 0))
        peak = max(peak, equity)
        dd = (equity - peak) / peak * 100 if peak else 0.0
        dd_max = min(dd_max, dd)
        r["drawdown_pct"] = round(dd, 2)
        curve.append({"date": r["date"], "equity": round(equity, 2), "dd": round(dd, 2)})
    net = sum(r.get("net", 0) for r in rows)
    traded = [r for r in rows if r.get("trades", 0) > 0]
    return {"ok": True, "capital": capital, "days": rows[::-1], "curve": curve,
            "summary": {"sessions": len(rows), "days_traded": len(traded),
                        "trades": sum(r.get("trades", 0) for r in rows),
                        "wins": sum(r.get("wins", 0) for r in rows),
                        "net": round(net, 2),
                        "gross": round(sum(r.get("gross", 0) for r in rows), 2),
                        "charges": round(sum(r.get("charges", 0) for r in rows), 2),
                        "return_pct": round(net / capital * 100, 2) if capital else None,
                        "equity": round(capital + net, 2), "max_dd_pct": round(dd_max, 2)}}


@router.get("/dates")
def dates():
    out = sorted({p.name[:10] for p in STATE.glob("????-??-??_STATE.json")}, reverse=True)
    return {"ok": True, "dates": out, "today": _now().date().isoformat()}
