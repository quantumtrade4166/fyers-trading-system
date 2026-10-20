"""
deployment/takerflow_api.py — what the ₿ Taker Flow tab reads.
==============================================================

The engine (live_trading_options/taker_flow/engine.py) is a SEPARATE process.
This router only reads the files it publishes: it never imports the engine and
never writes, so a broken dashboard cannot touch the paper book and a dead engine
cannot stop the dashboard starting.

    /api/takerflow/tick      equity, P&L split (price / fees / GST / funding), positions
    /api/takerflow/equity    cumulative P&L curve (5-min samples + every rebalance)
    /api/takerflow/trades    paper fills, newest first
    /api/takerflow/signal    the latest ranking: every coin, ratio, side, weight
    /api/takerflow/events    engine log, newest first

TAKERFLOW_STATE overrides the folder (local testing).
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/takerflow", tags=["taker-flow"])

STATE = Path(os.getenv("TAKERFLOW_STATE") or
             Path(__file__).resolve().parents[1] / "live_trading_options" / "taker_flow" / "data" / "state")
STALE_S = 180


def _read(name: str):
    for _ in range(3):                      # the engine may be mid-replace
        try:
            return json.loads((STATE / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.05)
    return None


def _lines(name: str) -> list[dict]:
    p = STATE / name
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue                    # a half-written last line
    return out


@router.get("/tick")
def tick():
    d = _read("TICK.json")
    if d is None:
        return {"ok": False, "stale": True, "reason": "engine has not published yet"}
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(d["ts"])).total_seconds()
    except Exception:
        age = None
    d.update(ok=True, age=round(age, 1) if age is not None else None,
             stale=age is None or age > STALE_S)
    return d


@router.get("/equity")
def equity(max_points: int = Query(2000, ge=10, le=20000)):
    rows = _lines("equity.jsonl")
    if len(rows) > max_points:
        step = len(rows) / max_points
        keep = {int(i * step) for i in range(max_points)} | {len(rows) - 1}
        rows = [r for i, r in enumerate(rows) if i in keep]
    return {"ok": True, "points": rows}


@router.get("/trades")
def trades(limit: int = Query(300, ge=1, le=5000)):
    rows = _lines("trades.jsonl")
    return {"ok": True, "count": len(rows), "trades": rows[-limit:][::-1]}


@router.get("/signal")
def signal():
    s = _read("signal.json") or {}
    rebs = _lines("rebalances.jsonl")
    return {"ok": bool(s), "asof": s.get("asof"), "universe": s.get("universe"),
            "eligible": s.get("eligible"), "longs": s.get("longs"), "shorts": s.get("shorts"),
            "table": sorted(s.get("table") or [], key=lambda r: -(r.get("taker_ratio") or 0)),
            "rebalances": [{k: r.get(k) for k in ("asof", "eligible", "orders", "complete", "equity_before")}
                           for r in rebs[-50:][::-1]]}


@router.get("/compare")
def compare():
    """Paper vs shadow backtest on the same days.

    Backtest points are equity at each UTC midnight (end of a daily bar). Paper is
    read at the same instants from its 5-minute samples (last sample at or before),
    so both columns describe the same stretch of time."""
    sh = _read("shadow.json")
    if not sh:
        return {"ok": False, "reason": "shadow backtest has not run yet (daily at 05:50 IST)"}
    eq = _lines("equity.jsonl")
    tick = _read("TICK.json") or {}
    cap = tick.get("capital") or (sh["points"][0]["equity"] if sh.get("points") else 10_000)
    times = [(datetime.fromisoformat(r["ts"]), r) for r in eq]
    days, prev_p, prev_b = [], 0.0, 0.0
    for p in sh.get("points", []):
        at = datetime.fromisoformat(p["date"] + "T00:00:00+00:00")
        paper = None
        for t, r in times:
            if t <= at:
                paper = r["pnl"]
            else:
                break
        if p["date"] == sh["start"]:
            paper = 0.0
        days.append({"date": p["date"], "backtest_pnl": p["pnl"], "paper_pnl": paper,
                     "backtest_day": round(p["pnl"] - prev_b, 2),
                     "paper_day": round(paper - prev_p, 2) if paper is not None else None,
                     "gap": round(paper - p["pnl"], 2) if paper is not None else None,
                     "backtest_cost": p["cost"], "backtest_funding": p["funding"]})
        prev_b = p["pnl"]
        if paper is not None:
            prev_p = paper
    paper_picks = {}
    for r in _lines("rebalances.jsonl"):
        paper_picks.setdefault((r.get("asof") or "")[:10], {"longs": r.get("longs"), "shorts": r.get("shorts")})
    picks = []
    for b in sh.get("picks", []):
        pp = paper_picks.get(b["rebalance"])
        same = None
        if pp:
            same = len(set(b["longs"]) & set(pp["longs"] or [])) + len(set(b["shorts"]) & set(pp["shorts"] or []))
        picks.append({**b, "paper_longs": pp and pp["longs"], "paper_shorts": pp and pp["shorts"],
                      "same": same, "of": len(b["longs"]) + len(b["shorts"])})
    return {"ok": True, "asof": sh.get("asof"), "start": sh.get("start"), "method": sh.get("method"),
            "cost_bps_median": sh.get("cost_bps_median"), "capital": cap,
            "days": days, "picks": picks[::-1]}


@router.get("/events")
def events(limit: int = Query(150, ge=1, le=2000)):
    return {"ok": True, "events": _lines("events.jsonl")[-limit:][::-1]}
