"""
deployment/btc_vwap_api.py — what the ₿ BTC VWAP tab reads.
===========================================================

Reads the files live_trading_options/delta_btc/vwap_engine.py publishes. Never
imports the strategy, so the dashboard cannot stall the books and a dead engine
cannot stop the dashboard starting. The ONE write: the arm/kill control file of
the live-capable version (ist_delta, "Vwap New Delta Ex"), which the engine reads.

A "date" is the day a CYCLE STARTED, same as the BTC Delta Neutral tab: the
full_expiry cycle of 15-Sep runs 15-Sep 17:35 -> 16-Sep 17:10 and is filed under
15-Sep, so one date = one chart, one VWAP, one MTM curve. Nothing is pruned —
every date stays selectable forever.

    /api/btcvwap/tick                    ~1s   live combined, VWAP, MTM per version
    /api/btcvwap/dates?version=                every date with data + summary
    /api/btcvwap/day?version=&date=            candles, strikes, trades, MTM curve
    /api/btcvwap/audit?version=&date=          that cycle's event log
    /api/btcvwap/state?version=                the live cycle (raw snapshot)
    /api/btcvwap/control          GET / POST   ist_delta arm (live) / paper / kill
"""

import json
import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Query, Body

router = APIRouter(prefix="/api/btcvwap", tags=["btcvwap"])

BTC = Path(__file__).resolve().parents[1] / "live_trading_options" / "delta_btc"
STATE = BTC / "data" / "vwap_state"
RESULTS = BTC / "data" / "vwap_results"
CHARTS = RESULTS / "charts"
LOGS = BTC / "logs"
VERSIONS = ("ist_day", "full_expiry", "ist_live", "ist_cap", "ist_delta")
LIVE_VERSION = "ist_delta"                    # the only version an ARM can reach
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
        rec["trades"] += int(r.get("trades") or r.get("leg_trades") or 0)
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


# ── arm / paper / kill for the live-capable version ──────────────────────
def _ctl_file():
    return STATE / f"live_control_{LIVE_VERSION}.json"


# Size limits for the dashboard's size/stop boxes (Delta wallet ~$1.2k on 2026-10-10;
# 1000 contracts/leg used ~$860 margin at 200x).
MAX_CONTRACTS = 2000
MAX_STOP_USD = 500.0


def _config_size():
    """ist_delta's contracts / stop from config/vwap_parameters.json (the default when
    the dashboard has not set its own)."""
    v = ((_read(BTC / "config" / "vwap_parameters.json", {}) or {}).get("versions") or {}).get(LIVE_VERSION) or {}
    return {"contracts": v.get("contracts"), "mtm_stop_usd": v.get("mtm_stop_usd")}


@router.get("/control")
def control():
    d = _read(_ctl_file(), {"mode": "paper", "kill": False}) or {}
    live = _live(LIVE_VERSION) or {}
    cfg = _config_size()
    # what the NEXT session will use; the running cycle keeps what it started with
    size = {"contracts": d.get("contracts") or cfg["contracts"],
            "mtm_stop_usd": d.get("mtm_stop_usd") or cfg["mtm_stop_usd"],
            "config": cfg, "max_contracts": MAX_CONTRACTS, "max_stop_usd": MAX_STOP_USD,
            "current": (live.get("params") or {})}
    return {"ok": True, "version": LIVE_VERSION, "control": d, "size": size,
            "engine_mode": live.get("mode"), "status": live.get("status"),
            "open_legs": [t for t, l in (live.get("legs") or {}).items() if l]}


@router.post("/control")
def set_control(body: dict = Body(...)):
    """{"action": "arm" | "paper" | "kill"} for ist_delta only. The engine applies a
    mode change only while flat; a kill closes every open leg at once.
    {"action": "size", "contracts": n, "mtm_stop_usd": x} sets the size and stop for
    the NEXT session (the engine applies it only while no cycle is running)."""
    action = str(body.get("action") or "").lower()
    d = _read(_ctl_file(), {"mode": "paper", "kill": False}) or {}
    # a KILL from an earlier day is already void for the engine; re-dating the file
    # below must not bring it back to life
    if d.get("kill") and str(d.get("updated") or "")[:10] != _now().date().isoformat():
        d["kill"] = False
    if action == "arm":
        d.update(mode="live", kill=False)
    elif action == "paper":
        d.update(mode="paper")
    elif action == "kill":
        d.update(kill=True)
    elif action == "size":
        try:
            c = int(body.get("contracts"))
            m = float(body.get("mtm_stop_usd"))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "contracts and stop must be numbers"}
        if not 1 <= c <= MAX_CONTRACTS:
            return {"ok": False, "reason": f"contracts must be 1-{MAX_CONTRACTS}"}
        if not 1 <= m <= MAX_STOP_USD:
            return {"ok": False, "reason": f"stop must be $1-${MAX_STOP_USD:g}"}
        d.update(contracts=c, mtm_stop_usd=m)
    else:
        return {"ok": False, "reason": "action must be arm, paper, kill or size"}
    d["updated"] = _now().isoformat(timespec="seconds")
    d["by"] = "dashboard"
    STATE.mkdir(parents=True, exist_ok=True)
    _ctl_file().write_text(json.dumps(d, indent=2), encoding="utf-8")
    return {"ok": True, "control": d}


# ── broker truth: what Delta itself says (read-only) ──────────────────────
# The book above is OUR record. This is Delta's: open positions, today's fills
# with the commission actually charged, and the wallet — plus a reconciliation of
# the two. Loaded by file path (several packages here have a `live` / `core`).
_BROKER = {"at": 0.0, "data": None}


def _delta_client():
    import importlib.util
    f = BTC / "live" / "delta_client.py"
    spec = importlib.util.spec_from_file_location("_btcvwap_delta_client", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DeltaClient()


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@router.get("/broker")
def broker(fresh: int = Query(0)):
    import time as _t
    if not fresh and _BROKER["data"] and _t.time() - _BROKER["at"] < 15:
        return _BROKER["data"]
    try:
        c = _delta_client()
        wallet = [w for w in (c.wallet() or []) if _num(w.get("balance"))]
        pos_raw = c.raw_positions()
        fills_raw = c.fills(page_size=100)
        tx_raw = c.transactions(page_size=200)
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    today = _now().date().isoformat()
    # our order ids, from the book (a Delta fill does not always echo client_order_id)
    book = _live(LIVE_VERSION) or {}
    our_ids = set()
    # + every live trade on file: after the cycle ends the live state no longer holds
    # today's trades, and Delta's fills do not echo our client_order_id
    filed = [r for r in _jsonl(RESULTS / "trades.jsonl")
             if r.get("version") == LIVE_VERSION and r.get("mode") == "live"]
    for rec in (book.get("trades") or []) + filed + [l for l in (book.get("legs") or {}).values() if l]:
        for o in rec.get("orders") or []:
            if o.get("id"):
                our_ids.add(str(o["id"]))
    positions = []
    for p in pos_raw:
        prod = p.get("product") or {}
        positions.append({"symbol": prod.get("symbol") or p.get("product_symbol"),
                          "product_id": p.get("product_id") or prod.get("id"),
                          "size": int(_num(p.get("size")) or 0),
                          "entry_price": _num(p.get("entry_price")),
                          "mark": _num(p.get("mark_price")),
                          "margin": _num(p.get("margin")),
                          "liquidation_price": _num(p.get("liquidation_price")),
                          "unrealized_pnl": _num(p.get("unrealized_pnl"))})
    fills = []
    for f in fills_raw:
        ts = str(f.get("created_at") or "")
        try:
            ist = (dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST)
                   .replace(tzinfo=None))
        except Exception:
            ist = None
        if ist is None or ist.date().isoformat() != today:
            continue
        meta = f.get("meta_data") or {}
        fills.append({"time": ist.strftime("%H:%M:%S"),
                      "symbol": f.get("product_symbol") or (f.get("product") or {}).get("symbol"),
                      "side": f.get("side"), "size": int(_num(f.get("size")) or 0),
                      "price": _num(f.get("price")), "fee": _num(f.get("commission")),
                      "role": f.get("role"), "order_id": f.get("order_id"),
                      "client_order_id": f.get("client_order_id") or meta.get("client_order_id"),
                      "ours": str(f.get("client_order_id") or meta.get("client_order_id") or "").startswith("bvw")
                              or str(f.get("order_id")) in our_ids})
    # reconciliation: our open legs vs Delta's positions
    recon = []
    for t, l in (book.get("legs") or {}).items():
        if not l or l.get("mode") != "live":
            continue
        d_size = next((p["size"] for p in positions if str(p["product_id"]) == str(l.get("product_id"))), 0)
        recon.append({"leg": t, "symbol": l.get("symbol"), "ours": -int(l.get("qty") or 0),
                      "delta": d_size, "ok": d_size == -int(l.get("qty") or 0)})
    # Delta's own ledger for today (BTC options only): premium cash in/out, fees, GST.
    # Net today = cashflow + commission (+ the cost to buy back anything still open).
    led = {"cashflow": 0.0, "fees_ex_gst": 0.0, "gst": 0.0, "charges": 0.0, "rows": 0}
    for t in tx_raw:
        md = t.get("meta_data") or {}
        sym = str(md.get("product_symbol") or "")
        if not (sym.startswith("C-BTC-") or sym.startswith("P-BTC-")):
            continue
        try:
            when = dt.datetime.fromisoformat(str(t.get("created_at")).replace("Z", "+00:00")).astimezone(IST)
        except Exception:
            continue
        if when.date().isoformat() != today:
            continue
        amt = _num(t.get("amount")) or 0.0
        led["rows"] += 1
        if t.get("transaction_type") == "cashflow":
            led["cashflow"] += amt
        elif t.get("transaction_type") == "commission":
            led["charges"] += amt
            led["gst"] += _num(md.get("gst")) or 0.0
            led["fees_ex_gst"] += _num(md.get("amount_without_gst")) or 0.0
    open_cost = sum(_num(p.get("unrealized_cashflow")) or 0.0 for p in pos_raw)
    led = {k: round(v, 4) for k, v in led.items()}
    led["open_buyback"] = round(open_cost, 4)
    led["net_today"] = round(led["cashflow"] + led["charges"] + open_cost, 4)
    usd = next((w for w in wallet if (w.get("asset_symbol") or "").upper() in ("USD", "USDT")), {})
    out = {"ok": True, "at": _now().strftime("%H:%M:%S"),
           "wallet": {"balance": _num(usd.get("balance")), "available": _num(usd.get("available_balance")),
                      "blocked_margin": _num(usd.get("blocked_margin")) or _num(usd.get("position_margin"))},
           "positions": positions, "fills": fills, "ledger": led,
           "fees_today": round(sum(f["fee"] or 0 for f in fills if f["ours"]), 6),
           "recon": recon, "recon_ok": all(r["ok"] for r in recon)}
    _BROKER.update(at=_t.time(), data=out)
    return out
