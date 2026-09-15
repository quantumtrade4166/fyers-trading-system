"""
Live book for Kotak Rohit - what the client actually holds, priced now.

SOURCES
    Broker (what the dashboard displays):
        holdings()   settled shares, with the broker's average price
        positions()  TODAY's trades. A CNC buy stays here until it settles (T+1);
                     holdings() is empty on the day of purchase - on 2026-09-15 it
                     returned "No holdings found" with 40 stocks bought.
        quotes()     LTP, day change
        limits()     free cash
    Ledger (the check):
        ledger.own_book() rebuilds every position from exchange fills alone, FIFO.

    Every position shows both. Quantity must agree to the share; if it does not,
    the row is flagged and the dashboard shows a reconciliation break rather than
    quietly trusting one side.

Nothing here places an order.
"""

import time
from datetime import datetime

import pytz

from deployment.dualmom_live import config as C
from deployment.dualmom_live import kotak_equity as K
from deployment.dualmom_live import ledger as L

IST = pytz.timezone("Asia/Kolkata")
_SERIES = ("-EQ", "-BE", "-BZ", "-SM", "-ST")
_cache = {"at": 0.0, "value": None}
CACHE_SECONDS = 20


def _base_symbol(s: str) -> str:
    s = str(s or "").strip().upper()
    for suf in _SERIES:
        if s.endswith(suf):
            return s[: -len(suf)]
    return s


def broker_positions(client) -> dict:
    """{symbol: {...}} merged from settled holdings and today's positions."""
    out = {}

    held = K.holdings(client)                   # {} on an empty / all-unsettled account
    for raw_sym, h in held.items():
        s = _base_symbol(raw_sym)
        out[s] = {"settled_qty": int(h["qty"]), "settled_avg": float(h["avg_price"] or 0),
                  "today_buy_qty": 0, "today_buy_amt": 0.0, "today_sell_qty": 0,
                  "today_sell_amt": 0.0, "trading_symbol": raw_sym, "series": None}

    resp = client.positions()
    rows = resp.get("data") if isinstance(resp, dict) else resp
    for r in rows or []:
        if str(r.get("prod", "")).upper() != "CNC" or str(r.get("exSeg")) != K.SEGMENT:
            continue
        s = _base_symbol(r.get("sym") or r.get("trdSym"))
        p = out.setdefault(s, {"settled_qty": 0, "settled_avg": 0.0, "today_buy_qty": 0,
                               "today_buy_amt": 0.0, "today_sell_qty": 0,
                               "today_sell_amt": 0.0})
        p["today_buy_qty"] += L._i(r.get("flBuyQty"))
        p["today_buy_amt"] += L._f(r.get("buyAmt"))
        p["today_sell_qty"] += L._i(r.get("flSellQty"))
        p["today_sell_amt"] += L._f(r.get("sellAmt"))
        p["trading_symbol"] = r.get("trdSym") or p.get("trading_symbol")
        p["series"] = r.get("series") or p.get("series")
        p["token"] = r.get("tok")

    for s, p in out.items():
        qty = p["settled_qty"] + p["today_buy_qty"] - p["today_sell_qty"]
        cost = p["settled_qty"] * p["settled_avg"] + p["today_buy_amt"]
        bought = p["settled_qty"] + p["today_buy_qty"]
        p["qty"] = qty
        # sells do not change the average cost of what remains
        p["avg_price"] = round(cost / bought, 4) if bought else 0.0
    return {s: p for s, p in out.items() if p["qty"] != 0}


def quotes(client, symbols, tokens: dict = None) -> dict:
    """{symbol: {ltp, change, change_pct, prev_close}} in one batched call per 25.

    `tokens` {symbol: exchange token} skips the scrip-master lookup. positions()
    already carries each stock's token; resolving 40 names from scratch took 65s
    on the first dashboard load after a restart (sandbox run, 2026-09-15).
    """
    tokens = tokens or {}
    token_of = {}
    for s in symbols:
        tok = tokens.get(s)
        if not tok:
            try:
                tok = K.resolve(client, s)["token"]
            except Exception:
                continue
        token_of[str(tok)] = s
    out, toks = {}, list(token_of)
    for i in range(0, len(toks), 25):
        req = [{"instrument_token": t, "exchange_segment": K.SEGMENT} for t in toks[i:i + 25]]
        try:
            rows = client.quotes(instrument_tokens=req)
        except Exception:
            continue
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("message") or []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            s = token_of.get(str(r.get("exchange_token")))
            if not s:
                continue
            ltp = L._f(r.get("ltp"))
            chg = L._f(r.get("change"))
            ohlc = r.get("ohlc") if isinstance(r.get("ohlc"), dict) else {}
            out[s] = {"ltp": ltp, "change": chg, "change_pct": L._f(r.get("per_change")),
                      "prev_close": round(ltp - chg, 4) if ltp else L._f(ohlc.get("close"))}
    return out


def benchmark_close():
    """NIFTY 50 last price from Fyers (the same data the signal uses). None if unavailable."""
    try:
        from deployment.dualmom_live import data_refresh as D
        r = D._connect().quotes({"symbols": "NSE:NIFTY50-INDEX"})
        d = (r.get("d") or [{}])[0].get("v") or {}
        return float(d.get("lp")) if d.get("lp") else None
    except Exception:
        return None


def live(client, use_cache: bool = True) -> dict:
    """Everything the dashboard shows, built fresh from the broker (20s cache)."""
    if use_cache and _cache["value"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return {**_cache["value"], "cached": True}

    now = datetime.now(IST)
    broker = broker_positions(client)
    own = L.own_book()
    ownpos = own["positions"]
    symbols = sorted(set(broker) | set(ownpos))
    q = quotes(client, symbols, {s: b.get("token") for s, b in broker.items() if b.get("token")})
    cash = float(K.cash_available(client))

    rows, mv, cost, day_pnl = [], 0.0, 0.0, 0.0
    breaks = []
    for s in symbols:
        b = broker.get(s, {})
        o = ownpos.get(s, {})
        qty = int(b.get("qty", 0))
        avg = float(b.get("avg_price") or o.get("avg_price") or 0)
        mk = q.get(s, {})
        ltp = mk.get("ltp") or avg
        value = qty * ltp
        pcost = qty * avg
        mtm = value - pcost
        # day P&L: vs today's buy price for shares bought today, vs prev close otherwise
        today_q = int(b.get("today_buy_qty", 0))
        today_avg = (b["today_buy_amt"] / today_q) if today_q else 0.0
        carried = qty - today_q
        d_pnl = today_q * (ltp - today_avg) + carried * float(mk.get("change") or 0)
        stop_px = avg * (1 - C.STOP_LOSS_PCT)
        recon_ok = qty == int(o.get("qty", 0))
        if not recon_ok:
            breaks.append({"symbol": s, "broker_qty": qty, "ledger_qty": o.get("qty", 0)})
        rows.append({
            "symbol": s, "trading_symbol": b.get("trading_symbol") or o.get("trading_symbol"),
            "series": b.get("series") or o.get("series"),
            "qty": qty, "avg_price": round(avg, 2), "ltp": round(ltp, 2),
            "value": round(value, 2), "cost": round(pcost, 2),
            "mtm": round(mtm, 2), "mtm_pct": round(mtm / pcost * 100, 3) if pcost else 0.0,
            "day_change_pct": round(float(mk.get("change_pct") or 0), 3),
            "day_pnl": round(d_pnl, 2), "settled_qty": b.get("settled_qty", 0),
            "unsettled_qty": today_q, "stop_price": round(stop_px, 2),
            "to_stop_pct": round((ltp / stop_px - 1) * 100, 2) if stop_px else None,
            "ledger_qty": o.get("qty", 0), "ledger_avg": o.get("avg_price"),
            "recon_ok": recon_ok, "priced": s in q,
            "first_fill": o.get("first_fill"), "charges_est": o.get("charges_est"),
        })
        mv += value
        cost += pcost
        day_pnl += d_pnl

    nav = mv + cash
    for r in rows:
        r["weight_pct"] = round(r["value"] / nav * 100, 3) if nav else 0.0
    rows.sort(key=lambda r: -r["value"])
    unreal = mv - cost

    # Charges not yet debited. Kotak's cash on 2026-09-15 equalled capital minus the
    # exact trade value to within 6 paise, i.e. STT/stamp/exchange charges had not
    # been taken yet - so broker NAV was ~Rs 1,167 better than reality. If cash
    # still matches "capital - buys + sells" (no charges), the ledger estimate is
    # shown as PENDING and a charges-adjusted NAV is given alongside. Once Kotak
    # debits them the match fails and pending drops to zero - never subtracted twice.
    fills_all = L._read("fills.jsonl")
    buys = sum(f["notional"] for f in fills_all if f["side"] == "BUY")
    sells = sum(f["notional"] for f in fills_all if f["side"] == "SELL")
    undebited = abs(cash - (C.CAPITAL_BASE - buys + sells)) <= 5.0 if fills_all else False
    pending = own["charges_est_total"] if undebited else 0.0
    out = {
        "ok": True, "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
        "account": C.CLIENT_ACCOUNT, "ucc": C.CLIENT_UCC,
        "capital_base": C.CAPITAL_BASE, "inception": C.INCEPTION_DATE,
        "nav": round(nav, 2), "cash": round(cash, 2), "market_value": round(mv, 2),
        "cost_basis": round(cost, 2),
        "deployed_pct": round(mv / nav * 100, 3) if nav else 0.0,
        "cash_pct": round(cash / nav * 100, 3) if nav else 0.0,
        "unrealized": round(unreal, 2),
        "unrealized_pct": round(unreal / cost * 100, 3) if cost else 0.0,
        "realized": own["realized"], "charges_est_total": own["charges_est_total"],
        "charges_pending_est": round(pending, 2),
        "nav_after_pending_charges": round(nav - pending, 2),
        "day_pnl": round(day_pnl, 2),
        "day_pnl_pct": round(day_pnl / (nav - day_pnl) * 100, 3) if nav - day_pnl else 0.0,
        "total_pnl": round(nav - C.CAPITAL_BASE, 2),
        "total_return_pct": round((nav / C.CAPITAL_BASE - 1) * 100, 3),
        "positions": len(rows), "unpriced": [r["symbol"] for r in rows if not r["priced"]],
        "reconciliation": {"ok": not breaks, "breaks": breaks,
                           "ledger_fills": own["fills"], "broker_positions": len(broker)},
        "rows": rows,
    }
    _cache.update(at=time.time(), value=out)
    return out


def snapshot_row(book: dict, benchmark=None) -> dict:
    """The NAV-series row for a book (intraday or end-of-day)."""
    dt = datetime.strptime(book["as_of"], "%Y-%m-%d %H:%M:%S")
    return {"date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M:%S"),
            "nav": book["nav"], "market_value": book["market_value"], "cash": book["cash"],
            "cost_basis": book["cost_basis"], "unrealized": book["unrealized"],
            "realized_cum": book["realized"], "charges_est_cum": book["charges_est_total"],
            "deployed_pct": book["deployed_pct"], "positions": book["positions"],
            "benchmark_close": benchmark, "return_pct": book["total_return_pct"]}


def equity_series() -> dict:
    """Inception + daily closes + today's intraday points, with drawdown."""
    pts = []
    try:
        t0 = IST.localize(datetime.strptime(C.INCEPTION_DATE + " 09:15:00", "%Y-%m-%d %H:%M:%S"))
        pts.append({"t": int(t0.timestamp()), "nav": float(C.CAPITAL_BASE), "kind": "inception"})
    except Exception:
        pass
    today = datetime.now(IST).strftime("%Y-%m-%d")
    for r in L.read_nav_daily():
        if r["date"] == today:
            continue                 # today comes from the intraday file below
        t = IST.localize(datetime.strptime(f"{r['date']} {r.get('time') or '15:30:00'}",
                                           "%Y-%m-%d %H:%M:%S"))
        pts.append({"t": int(t.timestamp()), "nav": float(r["nav"]), "kind": "eod",
                    "benchmark": r.get("benchmark_close") or None})
    for r in L.read_intraday(today):
        t = IST.localize(datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M:%S"))
        pts.append({"t": int(t.timestamp()), "nav": float(r["nav"]), "kind": "intraday"})
    pts.sort(key=lambda p: p["t"])
    dedup = {}
    for p in pts:
        dedup[p["t"]] = p
    pts = list(dedup.values())
    peak, dd = 0.0, []
    for p in pts:
        peak = max(peak, p["nav"])
        dd.append({"t": p["t"], "dd_pct": round((p["nav"] / peak - 1) * 100, 4) if peak else 0.0})
    max_dd = min((d["dd_pct"] for d in dd), default=0.0)
    return {"equity": pts, "drawdown": dd, "max_drawdown_pct": max_dd,
            "peak_nav": peak, "points": len(pts)}
