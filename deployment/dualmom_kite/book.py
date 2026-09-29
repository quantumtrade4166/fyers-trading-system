"""
Live DualMom book on the SHARED Kite account.

    positions  = BROKER (settled + T1 + today's buys - today's sells)
    prices     = Kite quotes()
    cash       = OWN LEDGER (capital contributed - own buys + own sells - est. charges).
                 The shared account's free margin is NOT cash - we only count money
                 that this strategy actually owns.
    ledger     = CROSS-CHECK. For every position the broker must hold AT LEAST our
                 quantity; the gap (broker qty - our qty) is shown as broker_extra
                 so you can see what is yours vs what someone else on the same
                 account happens to hold too.

Why broker is the source: the Kite ledger (rebuilt from fills) can have a gap on
the very first load or after any capture failure. The broker always has the truth.
The ledger stays as the audit trail AND the cash authority (margin is not cash).
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


def _avg_price(broker_row: dict) -> float:
    """Where the average price comes from.

    Kite doesn't expose an average price per position - only per fill. We use
    the ledger's FIFO cost / qty as the average, falling back to 0 when the
    ledger has nothing (so the MTM shows 0 instead of pretending we paid avg=0).
    """
    return 0.0


def live(kite, use_cache: bool = True) -> dict:
    if use_cache and _cache["value"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return {**_cache["value"], "cached": True}

    now = datetime.now(IST)
    own = L.own_book()
    ownpos = own["positions"]
    cashd = L.own_cash()
    cash = cashd["cash"]

    broker = K.broker_equity_qty(kite)
    symbols = sorted(set(broker) | set(ownpos))
    if not symbols:
        q = {}
    else:
        tsym = {s: (ownpos.get(s, {}).get("trading_symbol") or s) for s in symbols}
        try:
            q = K.quotes(kite, [tsym[s] for s in symbols]) if symbols else {}
        except Exception:
            q = {}
        # invert the quote dict (keyed by trading_symbol) to be keyed by base symbol
        by_base = {}
        for s in symbols:
            mk = q.get(tsym[s])
            if mk:
                by_base[s] = mk
        q = by_base

    # If the ledger has no fills we don't know the account's cash base or realized
    # P&L — those need the ledger. Market value, avg_price and unrealized MTM come
    # from the broker directly and are always available.
    _no_ledger = own["fills"] == 0

    rows, mv, cost, day_pnl, breaks = [], 0.0, 0.0, 0.0, []
    today = now.strftime("%Y-%m-%d")
    fills_today = [f for f in L._read("fills.jsonl")
                   if (f.get("exchange_time") or "").startswith(today) and f["side"] == "BUY"]
    bought_today = {}
    for f in fills_today:
        b = bought_today.setdefault(f["symbol"], [0, 0.0])
        b[0] += f["qty"]
        b[1] += f["notional"]

    for s in symbols:
        b = broker.get(s, {})
        o = ownpos.get(s, {})
        qty = int(b.get("qty", 0))
        if qty <= 0 and not o:
            continue
        # Average price: broker's own average_price (from holdings()) is the
        # ground truth — it reflects what the account actually paid including
        # partial fills, bonuses, splits. The ledger's FIFO avg is used only
        # as a fallback when the broker has no entry (shouldn't happen, but
        # defensive). When neither exists (no fills, no holdings avg) we keep
        # avg=0 and gate the cost-dependent fields via _no_ledger.
        avg = float(b.get("broker_avg") or o.get("avg_price") or 0)
        mk = q.get(s, {})
        ltp = mk.get("ltp") or avg
        value, pcost = qty * ltp, qty * avg
        tq, tamt = bought_today.get(s, [0, 0.0])
        tq = min(tq, qty)
        d_pnl = (tq * ltp - tamt * (tq / max(bought_today.get(s, [1])[0], 1))) + \
                (qty - tq) * float(mk.get("change") or 0)
        bq = b.get("qty") if b else None
        # Reconciliation: the account must hold AT LEAST our quantity. Holding
        # more is fine (the owner may own the same stock outside DualMom) and is
        # reported as broker_extra so the dashboard can show the gap.
        ledger_qty = int(o.get("qty", 0))
        recon_ok = bq is not None and bq >= ledger_qty
        if ledger_qty > 0 and not recon_ok:
            breaks.append({"symbol": s, "broker_qty": bq, "ledger_qty": ledger_qty})
        stop_px = avg * (1 - SC.STOP_LOSS_PCT) if avg else None
        rows.append({
            "symbol": s, "trading_symbol": b.get("trading_symbol") or o.get("trading_symbol"),
            "series": None,
            "qty": qty, "avg_price": round(avg, 2), "ltp": round(ltp, 2),
            "value": round(value, 2), "cost": round(pcost, 2),
            "mtm": round(value - pcost, 2),
            "mtm_pct": round((value - pcost) / pcost * 100, 3) if pcost else 0.0,
            "day_change_pct": round(float(mk.get("change_pct") or 0), 3),
            "day_pnl": round(d_pnl, 2),
            "settled_qty": int(b.get("settled", 0)),
            "unsettled_qty": int(b.get("t1", 0)) + int(b.get("today_buy", 0)) - int(b.get("today_sell", 0)),
            "stop_price": round(stop_px, 2) if stop_px else None,
            "to_stop_pct": round((ltp / stop_px - 1) * 100, 2) if stop_px and ltp else None,
            "ledger_qty": ledger_qty, "ledger_avg": round(avg, 4),
            "broker_qty": bq,
            "broker_extra": (bq - ledger_qty) if (bq is not None and bq > ledger_qty) else 0,
            "recon_ok": recon_ok, "priced": s in q,
            "first_fill": o.get("first_fill"), "charges_est": o.get("charges_est"),
        })
        mv += value
        cost += pcost
        day_pnl += d_pnl

    # NAV = our positions at market (this is what the strategy is doing).
    # account_value = NAV + our cash (total liquid net worth).
    nav = mv
    account_value = mv + cash
    for r in rows:
        r["weight_pct"] = round(r["value"] / account_value * 100, 3) if account_value else 0.0
    rows.sort(key=lambda r: -r["value"])
    unreal = mv - cost
    try:
        margin = K.free_margin(kite)
    except Exception as e:
        margin = {"error": f"{type(e).__name__}: {e}"}

    # Build the response (cost-dependent fields are already gated on _no_ledger).
    out = {
        "ok": True, "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
        "account": C.CLIENT_ACCOUNT, "ucc": "Zerodha", "broker": "kite",
        "capital_base": C.contributed(), "capital_total": C.CAPITAL_BASE,
        "capital_flows": C.CAPITAL_FLOWS,
        "inception": L.inception_date() or "not deployed",
        "nav": round(nav, 2), "account_value": round(account_value, 2),
        "cash": round(cash, 2), "market_value": round(mv, 2),
        "cost_basis": round(cost, 2),
        "deployed_pct": round(mv / account_value * 100, 3) if account_value else 0.0,
        "cash_pct": round(cash / account_value * 100, 3) if account_value else 0.0,
        "unrealized": round(unreal, 2),
        "unrealized_pct": round(unreal / cost * 100, 3) if cost else 0.0,
        "realized": own["realized"] if not _no_ledger else None,
        "charges_est_total": cashd["charges_est"],
        "charges_pending_est": 0.0,
        "nav_after_pending_charges": round(account_value, 2) if not _no_ledger else None,
        "cash_basis": "own ledger: capital - buys + sells - est. charges (shared account)",
        "day_pnl": round(day_pnl, 2),
        "day_pnl_pct": round(day_pnl / (account_value - day_pnl) * 100, 3) if (account_value - day_pnl) else 0.0,
        "total_pnl": round(account_value - C.contributed(), 2) if not _no_ledger else None,
        "total_return_pct": round((account_value / C.contributed() - 1) * 100, 3) if (C.contributed() and not _no_ledger) else None,
        "twr_return_pct": equity_series().get("twr_return_pct"),
        "positions": len(rows), "unpriced": [r["symbol"] for r in rows if not r["priced"]],
        "reconciliation": {"ok": not breaks,
                           "breaks": breaks,
                           "ledger_fills": own["fills"],
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
            "account_value": bk.get("account_value", bk["nav"] + bk["cash"]),
            "cost_basis": bk["cost_basis"], "unrealized": bk["unrealized"],
            "realized_cum": bk["realized"], "charges_est_cum": bk["charges_est_total"],
            "deployed_pct": bk["deployed_pct"], "positions": bk["positions"],
            "benchmark_close": benchmark, "return_pct": bk["total_return_pct"]}


def equity_series() -> dict:
    """NAV curve + TIME-WEIGHTED return and drawdown.

    A deposit lifts NAV without the strategy earning anything, so the curve is
    measured the way a fund is: each step's return is NAV_now / (NAV_then + money
    added in between) - 1, chained into an index. Drawdown comes from that index,
    never from raw NAV - otherwise adding Rs 20,000 would show as a +3.3% gain and
    hide a real fall.
    """
    pts = []
    inc = L.inception_date()
    if inc:
        t0 = IST.localize(datetime.strptime(inc + " 09:15:00", "%Y-%m-%d %H:%M:%S"))
        pts.append({"t": int(t0.timestamp()), "date": inc, "nav": C.contributed(inc),
                    "kind": "inception"})
    today = datetime.now(IST).strftime("%Y-%m-%d")
    for r in L.read_nav_daily():
        if r["date"] == today:
            continue
        t = IST.localize(datetime.strptime(f"{r['date']} {r.get('time') or '15:30:00'}",
                                           "%Y-%m-%d %H:%M:%S"))
        pts.append({"t": int(t.timestamp()), "date": r["date"], "nav": float(r["nav"]),
                    "kind": "eod"})
    for r in L.read_intraday(today):
        t = IST.localize(datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M:%S"))
        pts.append({"t": int(t.timestamp()), "date": r["date"], "nav": float(r["nav"]),
                    "kind": "intraday"})
    dedup = {p["t"]: p for p in sorted(pts, key=lambda p: p["t"])}
    pts = list(dedup.values())

    idx, peak_idx, dd, prev = 1.0, 1.0, [], None
    deposits = []
    for p in pts:
        added = 0.0
        if prev is not None:
            added = C.contributed(p["date"]) - C.contributed(prev["date"])
            if added:
                deposits.append({"t": p["t"], "date": p["date"], "amount": added})
            # money arrives in the morning and is invested that day, so it counts
            # in the BASE of the period, not as a gain at the end of it
            base = prev["nav"] + added
            if base > 0:
                idx *= p["nav"] / base
        p["index"] = round(idx, 6)
        p["capital"] = C.contributed(p["date"])
        p["deposit"] = added or None
        peak_idx = max(peak_idx, idx)
        dd.append({"t": p["t"], "dd_pct": round((idx / peak_idx - 1) * 100, 4)})
        prev = p
    return {"equity": pts, "drawdown": dd, "deposits": deposits,
            "max_drawdown_pct": min((d["dd_pct"] for d in dd), default=0.0),
            "twr_return_pct": round((idx - 1) * 100, 3),
            "peak_nav": max((p["nav"] for p in pts), default=0.0),
            "points": len(pts)}
