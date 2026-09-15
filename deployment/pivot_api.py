"""
deployment/pivot_api.py — what the Nifty Directional Pivot tab reads.
====================================================================

The engine (live_trading_options/nifty_pivot/engine.py) is a SEPARATE process.
This module only reads the files it publishes: it never imports the strategy,
never computes a signal, and never writes. A broken dashboard therefore cannot
touch the paper book, and a crashed engine cannot stop the dashboard starting.

    /api/ndp/tick     tiny, polled ~1s   spot, open position, live MTM, equity
    /api/ndp/state    ~3s               candles, pivots, Supertrend, markers, trades, log
    /api/ndp/daily    on load/exit      per-day summary -> equity curve + days table
    /api/ndp/dates    on load           days that have a chart to show
    /api/ndp/mtm      on load           intraday MTM samples for a day

Staleness is explicit: if the engine stops, its files stop changing and the tab
says STALE rather than presenting a frozen number as current.
"""

import json
import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/ndp", tags=["nifty-directional-pivot"])

BASE = Path(__file__).resolve().parents[1] / "live_trading_options" / "nifty_pivot"
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


def _age_hms(stamp: str) -> float | None:
    try:
        t = dt.datetime.strptime(stamp, "%H:%M:%S")
        n = _now()
        return (n - n.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)).total_seconds()
    except Exception:
        return None


@router.get("/tick")
def tick():
    d = _read(STATE / "TICK.json")
    if d is None:
        return {"ok": False, "stale": True, "reason": "engine has not published yet"}
    if d.get("date") != _now().date().isoformat():
        d.update(ok=True, stale=True, age=None, reason=f"last run was {d.get('date')}")
        return d
    age = _age_hms(d.get("ts", ""))
    d["age"] = round(age, 1) if age is not None else None
    d["stale"] = age is None or age > 15          # engine rewrites this every second
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


@router.get("/daily")
def daily():
    days = _read(RESULTS / "daily.json", {}) or {}
    params = _read(PARAMS, {}) or {}
    capital = params.get("capital", 2_000_000)
    rows = sorted(days.values(), key=lambda r: r["date"])
    equity, peak, dd_max = capital, capital, 0.0
    curve = []
    for r in rows:
        equity = r.get("equity_close", equity + r.get("net", 0))
        peak = max(peak, equity)
        dd = (equity - peak) / peak * 100 if peak else 0.0
        dd_max = min(dd_max, dd)
        r["drawdown_pct"] = round(dd, 2)
        curve.append({"date": r["date"], "equity": round(equity, 2), "dd": round(dd, 2)})
    traded = [r for r in rows if r.get("trades", 0) > 0]
    net = sum(r.get("net", 0) for r in rows)
    wins = sum(1 for r in traded if r.get("net", 0) > 0)
    return {
        "ok": True, "capital": capital, "days": rows[::-1], "curve": curve,
        "summary": {
            "sessions": len(rows), "days_traded": len(traded),
            "trades": sum(r.get("trades", 0) for r in rows),
            "net": round(net, 2), "gross": round(sum(r.get("gross", 0) for r in rows), 2),
            "costs": round(sum(r.get("charges", 0) + r.get("slippage", 0) for r in rows), 2),
            "return_pct": round(net / capital * 100, 2) if capital else None,
            "equity": round(capital + net, 2),
            "winning_days": wins, "max_dd_pct": round(dd_max, 2),
        },
    }


@router.get("/dates")
def dates():
    out = sorted({p.name[:10] for p in STATE.glob("????-??-??_STATE.json")}, reverse=True)
    return {"ok": True, "dates": out, "today": _now().date().isoformat()}


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
