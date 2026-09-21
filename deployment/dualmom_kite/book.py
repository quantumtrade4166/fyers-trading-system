"""
Live DualMom book on the SHARED Kite account.

    positions  = OUR fills only (FIFO own book) - the account's other holdings,
                 the strangle's options and manual trades are never counted
    cash       = capital - our buys + our sells - our estimated charges
    NAV        = our shares at market + our cash      (margin is never cash)

The broker is the CHECK, not the source: for every position the account must
hold AT LEAST our quantity. Holding more is fine (the owner may own the same
stock outside DualMom) and is shown; holding less is a reconciliation break.
"""

import time
from datetime import datetime

import pytz

from deployment.dualmom_kite import config as C
from deployment.dualmom_kite import kite_equity as K
from deployment.dualmom_kite import ledger as L
from deployment.dualmom_live import config as SC          # strategy constants (stop %)

IST = pytz.timezone("Asia/Kolkata")
_cache = {"at": 0.0, "value": None}
CACHE_SECONDS = 20


def live(kite, use_cache: bool = True) -> dict:
    if use_cache and _cache["value"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return {**_cache["value"], "cached": True}

    now = datetime.now(IST)
    own = L.own_book()
    pos = own["positions"]
    cashd = L.own_cash()
    cash = cashd["cash"]

    broker, broker_err = {}, None
    try:
        broker = K.broker_equity_qty(kite)
    except Exception as e:
        broker_err = f"{type(e).__name__}: {e}"

    tsym = {s: (p.get("trading_symbol") or s) for s, p in pos.items()}
    q = K.quotes(kite, list(tsym.values())) if tsym else {}

    today = now.strftime("%Y-%m-%d")
    fills_today = [f for f in L._read("fills.jsonl")
                   if (f.get("exchange_time") or "").startswith(today) and f["side"] == "BUY"]
    bought_today = {}
    for f in fills_today:
        b = bought_today.setdefault(f["symbol"], [0, 0.0])
        b[0] += f["qty"]
        b[1] += f["notional"]

    rows, mv, cost, day_pnl, breaks = [], 0.0, 0.0, 0.0, []
    for s, p in pos.items():
        qty, avg = int(p["qty"]), float(p["avg_price"])
        mk = q.get(tsym[s], {})
        ltp = mk.get("ltp") or avg
        value, pcost = qty * ltp, qty * avg
        tq, tamt = bought_today.get(s, [0, 0.0])
        tq = min(tq, qty)
        d_pnl = (tq * ltp - tamt * (tq / max(bought_today.get(s, [1])[0], 1))) + \
                (qty - tq) * float(mk.get("change") or 0)
        bq = broker.get(s, {}).get("qty") if broker else None
        recon_ok = broker_err is None and bq is not None and bq >= qty
        if not recon_ok:
            breaks.append({"symbol": s, "broker_qty": bq, "ledger_qty": qty})
        stop_px = avg * (1 - SC.STOP_LOSS_PCT)
        rows.append({
            "symbol": s, "trading_symbol": tsym[s], "series": p.get("series"),
            "qty": qty, "avg_price": round(avg, 2), "ltp": round(ltp, 2),
            "value": round(value, 2), "cost": round(pcost, 2),
            "mtm": round(value - pcost, 2),
            "mtm_pct": round((value - pcost) / pcost * 100, 3) if pcost else 0.0,
            "day_change_pct": round(float(mk.get("change_pct") or 0), 3),
            "day_pnl": round(d_pnl, 2),
            "settled_qty": qty - tq, "unsettled_qty": tq,
            "stop_price": round(stop_px, 2),
            "to_stop_pct": round((ltp / stop_px - 1) * 100, 2) if stop_px else None,
            "ledger_qty": qty, "ledger_avg": round(avg, 4), "broker_qty": bq,
            "broker_extra": (bq - qty) if (bq is not None and bq > qty) else 0,
            "recon_ok": recon_ok, "priced": tsym[s] in q,
            "first_fill": p.get("first_fill"), "charges_est": p.get("charges_est"),
        })
        mv += value
        cost += pcost
        day_pnl += d_pnl

    nav = mv + cash
    for r in rows:
        r["weight_pct"] = round(r["value"] / nav * 100, 3) if nav else 0.0
    rows.sort(key=lambda r: -r["value"])
    unreal = mv - cost
    try:
        margin = K.free_margin(kite)
    except Exception as e:
        margin = {"error": f"{type(e).__name__}: {e}"}

    out = {
        "ok": True, "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
        "account": C.CLIENT_ACCOUNT, "ucc": "Zerodha", "broker": "kite",
        "capital_base": C.CAPITAL_BASE, "inception": L.inception_date() or "not deployed",
        "nav": round(nav, 2), "cash": round(cash, 2), "market_value": round(mv, 2),
        "cost_basis": round(cost, 2),
        "deployed_pct": round(mv / nav * 100, 3) if nav else 0.0,
        "cash_pct": round(cash / nav * 100, 3) if nav else 0.0,
        "unrealized": round(unreal, 2),
        "unrealized_pct": round(unreal / cost * 100, 3) if cost else 0.0,
        "realized": own["realized"], "charges_est_total": cashd["charges_est"],
        "charges_pending_est": 0.0, "nav_after_pending_charges": round(nav, 2),
        "cash_basis": "own ledger: capital - buys + sells - est. charges (shared account)",
        "day_pnl": round(day_pnl, 2),
        "day_pnl_pct": round(day_pnl / (nav - day_pnl) * 100, 3) if nav - day_pnl else 0.0,
        "total_pnl": round(nav - C.CAPITAL_BASE, 2),
        "total_return_pct": round((nav / C.CAPITAL_BASE - 1) * 100, 3),
        "positions": len(rows), "unpriced": [r["symbol"] for r in rows if not r["priced"]],
        "reconciliation": {"ok": not breaks and broker_err is None, "breaks": breaks,
                           "error": broker_err, "ledger_fills": own["fills"],
                           "broker_positions": len(broker),
                           "rule": "account must hold AT LEAST DualMom's quantity"},
        "account_margin": margin,
        "rows": rows,
    }
    _cache.update(at=time.time(), value=out)
    return out


def snapshot_row(bk: dict, benchmark=None) -> dict:
    dt = datetime.strptime(bk["as_of"], "%Y-%m-%d %H:%M:%S")
    return {"date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M:%S"),
            "nav": bk["nav"], "market_value": bk["market_value"], "cash": bk["cash"],
            "cost_basis": bk["cost_basis"], "unrealized": bk["unrealized"],
            "realized_cum": bk["realized"], "charges_est_cum": bk["charges_est_total"],
            "deployed_pct": bk["deployed_pct"], "positions": bk["positions"],
            "benchmark_close": benchmark, "return_pct": bk["total_return_pct"]}


def equity_series() -> dict:
    pts = []
    inc = L.inception_date()
    if inc:
        t0 = IST.localize(datetime.strptime(inc + " 09:15:00", "%Y-%m-%d %H:%M:%S"))
        pts.append({"t": int(t0.timestamp()), "nav": float(C.CAPITAL_BASE), "kind": "inception"})
    today = datetime.now(IST).strftime("%Y-%m-%d")
    for r in L.read_nav_daily():
        if r["date"] == today:
            continue
        t = IST.localize(datetime.strptime(f"{r['date']} {r.get('time') or '15:30:00'}",
                                           "%Y-%m-%d %H:%M:%S"))
        pts.append({"t": int(t.timestamp()), "nav": float(r["nav"]), "kind": "eod"})
    for r in L.read_intraday(today):
        t = IST.localize(datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M:%S"))
        pts.append({"t": int(t.timestamp()), "nav": float(r["nav"]), "kind": "intraday"})
    dedup = {p["t"]: p for p in sorted(pts, key=lambda p: p["t"])}
    pts = list(dedup.values())
    peak, dd = 0.0, []
    for p in pts:
        peak = max(peak, p["nav"])
        dd.append({"t": p["t"], "dd_pct": round((p["nav"] / peak - 1) * 100, 4) if peak else 0.0})
    return {"equity": pts, "drawdown": dd,
            "max_drawdown_pct": min((d["dd_pct"] for d in dd), default=0.0),
            "peak_nav": peak, "points": len(pts)}
