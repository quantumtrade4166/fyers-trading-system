"""
deployment/btc_vwap_api.py — what the ₿ BTC VWAP tab reads.
===========================================================

Read-only over the files live_trading_options/delta_btc/vwap_engine.py publishes.
Never imports the strategy and never writes, so the dashboard cannot stall the
paper books and a dead engine cannot stop the dashboard starting.

A "date" is the day a CYCLE STARTED, same as the BTC Delta Neutral tab: the
full_expiry cycle of 15-Sep runs 15-Sep 17:35 -> 16-Sep 17:10 and is filed under
15-Sep, so one date = one chart, one VWAP, one MTM curve. Nothing is pruned —
every date stays selectable forever.

    /api/btcvwap/tick                    ~1s   live combined, VWAP, MTM per version
    /api/btcvwap/dates?version=                every date with data + summary
    /api/btcvwap/day?version=&date=            candles, strikes, trades, MTM curve
    /api/btcvwap/audit?version=&date=          that cycle's event log
    /api/btcvwap/state?version=                the live cycle (raw snapshot)
"""

import json
import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/btcvwap", tags=["btcvwap"])

BTC = Path(__file__).resolve().parents[1] / "live_trading_options" / "delta_btc"
STATE = BTC / "data" / "vwap_state"
RESULTS = BTC / "data" / "vwap_results"
CHARTS = RESULTS / "charts"
LOGS = BTC / "logs"
VERSIONS = ("ist_day", "full_expiry")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _now():
    return dt.datetime.now(IST).replace(tzinfo=None)


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _jsonl(path: Path) -> list:
    rows = []
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
    return rows


def _live(version: str):
    d = _read(STATE / f"{version}.json")
    if not d:
        return None
    try:
        age = (_now() - dt.datetime.strptime(d["updated"], "%Y-%m-%d %H:%M:%S")).total_seconds()
    except Exception:
        age = None
    d.update(ok=True, age=age, stale=age is None or age > 60, live=True)
    return d


@router.get("/tick")
def tick():
    d = _read(STATE / "TICK.json")
    if d is None:
        return {"ok": False, "stale": True, "reason": "engine has not written a tick yet"}
    try:
        t = dt.datetime.strptime(d.get("ts", ""), "%H:%M:%S")
        now = _now()
        age = (now - now.replace(hour=t.hour, minute=t.minute, second=t.second,
                                 microsecond=0)).total_seconds() % 86400
    except Exception:
        age = None
    d.update(ok=True, age=age, stale=age is None or age > 20)
    return d


@router.get("/state")
def state(version: str = Query("full_expiry")):
    if version not in VERSIONS:
        return {"ok": False, "reason": "unknown version"}
    return _live(version) or {"ok": False, "stale": True, "reason": "engine has not written state yet"}


@router.get("/dates")
def dates(version: str = Query("full_expiry")):
    """Every cycle-start date for this version, newest first."""
    days: dict = {}

    def touch(d):
        return days.setdefault(d, {"date": d, "trades": 0, "pnl": 0.0, "live": False,
                                   "pair": None})

    for r in _jsonl(RESULTS / "cycles.jsonl"):
        if r.get("version") != version or not r.get("cycle"):
            continue
        rec = touch(r["cycle"][:10])
        rec["trades"] += int(r.get("trades") or 0)
        rec["pnl"] = round(rec["pnl"] + float(r.get("realized") or 0), 4)
        rec["pair"] = f"{r.get('ce'):.0f}CE/{r.get('pe'):.0f}PE" if r.get("ce") else None
    for f in CHARTS.glob(f"{version}_*.json") if CHARTS.exists() else []:
        stem = f.stem[len(version) + 1:]            # 2026-09-15T1735
        if len(stem) >= 10:
            touch(stem[:10])
    live = _live(version)
    if live and live.get("cycle"):
        rec = touch(live["cycle"][:10])
        rec["live"] = True
        rec["trades"] = len(live.get("trades") or [])
        rec["pnl"] = round(float(live.get("mtm") or 0), 4)
        p = live.get("pair") or {}
        rec["pair"] = f"{p.get('ce'):.0f}CE/{p.get('pe'):.0f}PE" if p.get("ce") else None
    today = _now().date().isoformat()
    if not days:
        touch(today)
    return {"ok": True, "today": today,
            "days": sorted(days.values(), key=lambda x: x["date"], reverse=True)}


def _equity(version: str, cycle: str) -> list:
    out, seen = [], set()
    for r in _jsonl(RESULTS / f"{version}_equity.jsonl"):
        if r.get("cycle") != cycle or r.get("ts") in seen:
            continue
        seen.add(r.get("ts"))
        out.append({"t": r["ts"], "mtm": r.get("mtm"), "realized": r.get("realized"),
                    "in_pos": r.get("in_pos")})
    return out


@router.get("/day")
def day(version: str = Query("full_expiry"), date: str = Query(None)):
    """The cycle that started on `date`: the live one if it is still running,
    otherwise its archived chart. Always carries that cycle's MTM curve."""
    if version not in VERSIONS:
        return {"ok": False, "reason": "unknown version"}
    live = _live(version)
    date = date or (live["cycle"][:10] if live and live.get("cycle") else _now().date().isoformat())
    if live and (live.get("cycle") or "").startswith(date):
        d = live
    else:
        files = sorted(CHARTS.glob(f"{version}_{date}T*.json")) if CHARTS.exists() else []
        d = _read(files[-1]) if files else None
        if d is None:
            return {"ok": True, "date": date, "empty": True, "live": False,
                    "reason": "no cycle started on this date",
                    "status": live.get("status") if live else None,
                    "candles": [], "trades": [], "equity": []}
        d.update(ok=True, live=False)
        summ = [r for r in _jsonl(RESULTS / "cycles.jsonl")
                if r.get("version") == version and r.get("cycle") == d.get("cycle")]
        if summ:
            d["summary"] = summ[-1]
    d["date"] = date
    d["equity"] = _equity(version, d.get("cycle")) if d.get("cycle") else []
    return d


@router.get("/audit")
def audit(version: str = Query(None), date: str = Query(None),
          limit: int = Query(300, ge=1, le=5000)):
    """Events of the cycle that started on `date` (a full_expiry cycle spans two
    log files, so both days are read and filtered to that cycle)."""
    date = date or _now().date().isoformat()
    try:
        nxt = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
    except ValueError:
        return {"ok": False, "reason": "bad date"}
    rows = []
    for d in (date, nxt):
        rows += [r for r in _jsonl(LOGS / f"{d}_btc_vwap_audit.log")
                 if (not version or r.get("version") == version)
                 and str(r.get("cycle") or "").startswith(date)]
    return {"ok": True, "date": date, "events": rows[-limit:], "total": len(rows)}
