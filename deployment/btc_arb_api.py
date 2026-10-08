"""
deployment/btc_arb_api.py — what the ₿ BTC Arbitrage tab reads.
==============================================================

Read-only over the files crypto/btc_arbitrage/main.py publishes. Never imports
the monitor's runtime and never writes, so a dead monitor cannot stop the
dashboard starting and a slow dashboard cannot stall the monitor.

    /api/btcarb/tick                 ~4/s  latest quotes, spread, status, recent line
    /api/btcarb/series?window=15m          downsampled history + window stats
    /api/btcarb/events?limit=100           opportunity + connection events, newest first
    /api/btcarb/paper                      zero-fee paper trader: control, position, trades
    POST /api/btcarb/paper/control         the tab's toggle + parameters (PAPER ONLY —
                                           the only thing this router ever writes)
"""

import os
import json
import time
import sqlite3
import importlib.util
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Query, Body

router = APIRouter(prefix="/api/btcarb", tags=["btcarb"])

ARB = Path(__file__).resolve().parents[1] / "crypto" / "btc_arbitrage"
TICK = ARB / "data" / "state" / "TICK.json"
EVENTS = ARB / "data" / "state" / "events.jsonl"
DB_DIR = ARB / "data" / "spread_db"
PAPER_CONTROL = ARB / "data" / "state" / "paper_control.json"
PAPER_TRADES = ARB / "data" / "paper" / "trades.jsonl"
# same defaults / limits as crypto/btc_arbitrage/core/paper.py (the monitor
# re-validates whatever is written here, so a mismatch can only be refused)
PAPER_DEFAULTS = {"enabled": False, "entry_usd": 20.0, "exit_band_usd": 1.0, "latency_ms": 300}
PAPER_LIMITS = {"entry_usd": (0.5, 10_000.0), "exit_band_usd": (0.0, 1_000.0), "latency_ms": (0, 5_000)}
WINDOWS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
MAX_POINTS = 1500

# storage/database.py is pure sqlite; load it by path so its package's
# `config` / `core` names can never collide with the dashboard's own modules.
_spec = importlib.util.spec_from_file_location("btcarb_database", ARB / "storage" / "database.py")
_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_db)


def _read(path: Path):
    for _ in range(3):                      # the monitor may be mid-replace
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.02)
    return None


@router.get("/tick")
def tick():
    d = _read(TICK)
    if d is None:
        return {"ok": False, "stale": True, "reason": "monitor has not written a tick yet"}
    age = time.time() - (d.get("ts_ms") or 0) / 1000
    d["age_s"] = round(age, 1)
    d["stale"] = age > 5
    return d


@router.get("/series")
def series(window: str = Query("15m"), points: bool = Query(True)):
    span = WINDOWS.get(window, 900)
    end = int(time.time())
    start = end - span
    out = _db.read_series(DB_DIR, start, end, MAX_POINTS) if points else {"points": []}
    out.update(window=window, span_s=span, start=start, end=end, stats=_stats(start, end))
    return out


def _stats(start: int, end: int) -> dict:
    """How often, and how far, the books actually crossed in this window."""
    agg = {"seconds": 0, "ok_seconds": 0, "crossed_buy_delta": 0, "crossed_buy_binance": 0,
           "max_s1": None, "max_s2": None, "max_net1": None, "max_net2": None,
           "opp_seconds": 0, "mid_sum": 0.0, "mid_sq": 0.0, "mid_min": None, "mid_max": None}
    for day in sorted({_db.ist_date(start), _db.ist_date(end)}):
        p = DB_DIR / f"{day}.sqlite"
        if not p.exists():
            continue
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            r = con.execute(
                "SELECT COUNT(*), SUM(quality='OK'), SUM(quality='OK' AND s1_max>0), "
                "SUM(quality='OK' AND s2_max>0), MAX(s1_max), MAX(s2_max), MAX(net1_max), "
                "MAX(net2_max), SUM(opportunity), SUM(mid_spread), SUM(mid_spread*mid_spread), "
                "MIN(mid_min), MAX(mid_max) FROM snap WHERE t BETWEEN ? AND ?", (start, end)).fetchone()
        except sqlite3.OperationalError:
            r = None
        finally:
            con.close()
        if not r or not r[0]:
            continue
        agg["seconds"] += r[0]
        agg["ok_seconds"] += r[1] or 0
        agg["crossed_buy_delta"] += r[2] or 0
        agg["crossed_buy_binance"] += r[3] or 0
        for k, v, f in (("max_s1", r[4], max), ("max_s2", r[5], max), ("max_net1", r[6], max),
                        ("max_net2", r[7], max), ("mid_min", r[11], min), ("mid_max", r[12], max)):
            if v is not None:
                agg[k] = v if agg[k] is None else f(agg[k], v)
        agg["opp_seconds"] += r[8] or 0
        agg["mid_sum"] += r[9] or 0
        agg["mid_sq"] += r[10] or 0
    n = agg.pop("seconds")
    s, sq = agg.pop("mid_sum"), agg.pop("mid_sq")
    agg["seconds"] = n
    agg["mid_mean"] = s / n if n else None
    agg["mid_std"] = max(0.0, sq / n - (s / n) ** 2) ** 0.5 if n else None
    return agg


@router.get("/events")
def events(limit: int = Query(100, ge=1, le=1000)):
    if not EVENTS.exists():
        return {"events": []}
    tail = deque(maxlen=limit)
    try:
        with open(EVENTS, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("{"):
                    tail.append(line)
    except OSError:
        return {"events": []}
    out = []
    for line in reversed(tail):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return {"events": out}


# ── paper trading ────────────────────────────────────────────────────────
@router.get("/paper")
def paper(limit: int = Query(200, ge=1, le=5000)):
    t = _read(TICK) or {}
    trades = []
    if PAPER_TRADES.exists():
        try:
            for line in PAPER_TRADES.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("{"):
                    try:
                        trades.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
    n = len(trades)
    pnl = sum(x.get("pnl") or 0 for x in trades)
    pnl_fees = sum(x.get("pnl_with_fees") or 0 for x in trades)
    wins = sum(1 for x in trades if (x.get("pnl") or 0) > 0)
    curve, cum, cum_f = [], 0.0, 0.0
    for x in trades:
        cum += x.get("pnl") or 0
        cum_f += x.get("pnl_with_fees") or 0
        curve.append({"t": int((x.get("exit_ms") or 0) / 1000), "pnl": round(cum, 2), "pnl_fees": round(cum_f, 2)})
    summary = {"trades": n, "wins": wins, "win_rate": round(wins / n * 100, 1) if n else None,
               "pnl": round(pnl, 2), "pnl_with_fees": round(pnl_fees, 2),
               "avg_pnl": round(pnl / n, 2) if n else None,
               "avg_hold_s": round(sum(x.get("hold_s") or 0 for x in trades) / n, 1) if n else None,
               "avg_slippage": round(sum(x.get("slippage_vs_signal") or 0 for x in trades) / n, 2) if n else None,
               "best": max((x.get("pnl") or 0 for x in trades), default=None),
               "worst": min((x.get("pnl") or 0 for x in trades), default=None)}
    return {"ok": bool(t), "stale": (time.time() - (t.get("ts_ms") or 0) / 1000) > 5 if t else True,
            "live": t.get("paper"), "control_file": _read(PAPER_CONTROL) or dict(PAPER_DEFAULTS),
            "summary": summary, "curve": curve, "trades": list(reversed(trades))[:limit]}


@router.post("/paper/control")
def paper_control(body: dict = Body(...)):
    """PAPER ONLY: switches the zero-fee paper trader and its rules. Cannot touch
    any exchange — the monitor has no order code."""
    cur = _read(PAPER_CONTROL) or dict(PAPER_DEFAULTS)
    new = dict(PAPER_DEFAULTS)
    new.update({k: cur.get(k, v) for k, v in PAPER_DEFAULTS.items()})
    errors = []
    if "enabled" in body:
        new["enabled"] = bool(body["enabled"])
    for k, (lo, hi) in PAPER_LIMITS.items():
        if k in body:
            try:
                v = float(body[k])
            except (TypeError, ValueError):
                errors.append(f"{k}: not a number")
                continue
            if not lo <= v <= hi:
                errors.append(f"{k}: must be between {lo:g} and {hi:g}")
                continue
            new[k] = int(v) if k == "latency_ms" else v
    if errors:
        return {"ok": False, "errors": errors, "control": cur}
    PAPER_CONTROL.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAPER_CONTROL.with_suffix(".tmp")
    tmp.write_text(json.dumps(new), encoding="utf-8")
    for _ in range(5):
        try:
            os.replace(tmp, PAPER_CONTROL)
            break
        except PermissionError:
            time.sleep(0.05)
    else:
        PAPER_CONTROL.write_text(json.dumps(new), encoding="utf-8")
    return {"ok": True, "control": new}
