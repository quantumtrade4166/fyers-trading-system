"""
dualmom_live/rebalance.py — turn a signal + current holdings into an order list.

Two pieces:
  allocate()  target share counts from NAV and weights (identical maths to the
              canonical backtest `dualmom_final.allocate`, so live and backtest
              cannot silently diverge)
  plan()      DELTA rebalance — trade only the difference, sells before buys

Allocation rules (agreed 2026-09-01, measured at Rs 10L: 40/40 names held every
invested month since 2021, idle cash 0.005%):
  1. whole shares only
  2. a name unaffordable at its target weight is SKIPPED and its weight
     redistributed across the rest — never left as idle cash
  3. leftover rupees go to whichever holding sits furthest BELOW target
  4. never buy a share that pushes a position past 2x its target weight

Naive flooring instead leaves 9.1% idle and silently drops the 3 most expensive
names (POWERINDIA Rs 33,760, NEULANDLAB Rs 23,010, PTCIL Rs 22,114).

T+1 NOTE: sell proceeds are not fully available as buying power the same day.
plan() therefore emits sells first and marks buys with `needs_proceeds` so the
executor can wait for the sells to confirm before sizing the buys.
"""

import math

from . import config as C


def cap_weights(raw: dict, wcap: float) -> dict:
    """Cap every weight at `wcap`, redistributing the excess pro-rata to the names
    still under it. Iterates because redistribution can push another name over.

    MUST stay identical to dualmom_final.cap_weights -- test_parity asserts it.
    An earlier attempt added the excess to names ALREADY at the cap, which pushed
    the total above 1.0 and silently levered the book (CAGR rose while Sharpe
    collapsed). The caller asserts the total is 1.0.
    """
    if not raw:
        return {}
    tot = sum(raw.values())
    base = {s: v / tot for s, v in raw.items()}
    if wcap is None or wcap >= 1.0:
        return base
    if wcap * len(base) <= 1.0:
        return {s: 1.0 / len(base) for s in base}

    capped = set()
    while True:
        others = [s for s in base if s not in capped]
        remaining = 1.0 - len(capped) * wcap
        if not others or remaining <= 0:
            return {s: 1.0 / len(base) for s in base}
        sub = sum(base[s] for s in others)
        w = {s: wcap for s in capped}
        newly = []
        for s in others:
            w[s] = remaining * base[s] / sub
            if w[s] > wcap + 1e-12:
                newly.append(s)
        if not newly:
            return w
        capped.update(newly)


def allocate(weights: dict, price: dict, capital: float,
             max_mult: float = None) -> dict:
    """{symbol: shares}. `price` must already include the slippage/limit uplift."""
    max_mult = C.MAX_WEIGHT_MULT if max_mult is None else max_mult

    afford = {s: w for s, w in weights.items()
              if price.get(s, 0) > 0 and price[s] <= capital * w * max_mult}
    if not afford:
        return {}
    tot = sum(afford.values())
    afford = {s: w / tot for s, w in afford.items()}

    q = {s: math.floor(capital * w / price[s]) for s, w in afford.items()}
    cash = capital - sum(q[s] * price[s] for s in q)

    for _ in range(5000):                     # greedy fill of the remainder
        best, gap = None, 0.0
        for s, w in afford.items():
            p = price[s]
            if p > cash or (q[s] + 1) * p > capital * w * max_mult:
                continue
            short = capital * w - q[s] * p
            if short > gap:
                best, gap = s, short
        if best is None:
            break
        q[best] += 1
        cash -= price[best]
    return {s: n for s, n in q.items() if n > 0}


def plan(signal: dict, current: dict, marks: dict, nav: float,
         reserve: float = None) -> dict:
    """Build the order list.

    signal   from signal_engine.compute()
    current  {symbol: qty} we hold now
    marks    {symbol: last price}
    nav      capital to deploy
    reserve  rupees held back from sizing (default config.CASH_RESERVE_RS; the
             Kite account passes its own)
    """
    sells, buys, skipped = [], [], []

    if signal["signal"] == "OUT":
        for sym, qty in sorted(current.items()):
            if qty > 0:
                mark = float(marks.get(sym, 0))
                sells.append({"symbol": sym, "side": "SELL", "qty": int(qty),
                              "mark": mark, "value": round(abs(qty) * mark, 2),
                              "target_qty": 0, "current_qty": int(qty),
                              "reason": "regime OUT — full exit to cash"})
        return _finalise(signal, sells, buys, skipped, current, {}, nav)

    weights = {h["symbol"]: h["weight"] for h in signal["holdings"]}
    px = {s: float(marks[s]) for s in weights if marks.get(s, 0) > 0}
    missing = [s for s in weights if s not in px]
    for s in missing:
        skipped.append({"symbol": s, "reason": "no live price"})
    if missing:                                # redistribute the lost weight
        weights = {s: w for s, w in weights.items() if s in px}
        tot = sum(weights.values())
        weights = {s: w / tot for s, w in weights.items()}

    # size off EXPECTED fill (matches the backtest), never off the limit cap
    eff = {s: p * (1 + C.SIZING_SLIPPAGE) for s, p in px.items()}
    # hold back CASH_RESERVE_RS for charges + the limit cap (see config)
    reserve = C.CASH_RESERVE_RS if reserve is None else reserve
    target = allocate(weights, eff, max(nav - reserve, 0.0))

    for sym in sorted(set(current) | set(target)):
        have, want = int(current.get(sym, 0)), int(target.get(sym, 0))
        delta = want - have
        if delta == 0:
            continue
        mark = float(marks.get(sym, px.get(sym, 0)))
        value = abs(delta) * mark
        if value < C.MIN_ORDER_VALUE:
            skipped.append({"symbol": sym, "reason": f"below min order value (Rs {value:,.0f})",
                            "delta": delta})
            continue
        row = {"symbol": sym, "qty": abs(delta), "mark": mark,
               "value": round(value, 2),
               "target_qty": want, "current_qty": have}
        if delta < 0:
            sells.append({**row, "side": "SELL",
                          "reason": "exit" if want == 0 else "trim to target"})
        else:
            buys.append({**row, "side": "BUY", "needs_proceeds": True,
                         "reason": "new" if have == 0 else "top up to target"})

    return _finalise(signal, sells, buys, skipped, current, target, nav)


def _finalise(signal, sells, buys, skipped, current, target, nav) -> dict:
    sells.sort(key=lambda r: -r["value"])
    buys.sort(key=lambda r: -r["value"])
    sell_val = sum(r["value"] for r in sells)
    buy_val = sum(r["value"] for r in buys)
    turnover = (sell_val + buy_val) / (2 * nav) if nav > 0 else 0.0

    out = {
        "signal_date": signal["date"],
        "signal": signal["signal"],
        "nav": round(nav, 2),
        "sells": sells, "buys": buys, "skipped": skipped,
        "n_orders": len(sells) + len(buys),
        "sell_value": round(sell_val, 2),
        "buy_value": round(buy_val, 2),
        "turnover_pct": round(turnover * 100, 2),
        "target_names": len(target),
        "signal_names": len(signal.get("holdings", [])),
        "blocked": [],
    }
    out["blocked"] = _safety_checks(out)
    out["safe"] = not out["blocked"]
    return out


def _safety_checks(plan: dict) -> list:
    """Hard brakes for an unattended run. If the plan looks insane, something
    upstream broke — halt rather than execute."""
    bad = []
    if plan["n_orders"] > C.MAX_ORDERS_PER_RUN:
        bad.append(f"{plan['n_orders']} orders exceeds MAX_ORDERS_PER_RUN "
                   f"({C.MAX_ORDERS_PER_RUN})")
    if plan["signal"] == "IN" and plan["turnover_pct"] / 100 > C.MAX_TURNOVER_PCT:
        bad.append(f"turnover {plan['turnover_pct']:.0f}% exceeds "
                   f"{C.MAX_TURNOVER_PCT*100:.0f}% — expected ~26%/month")
    want = plan.get("signal_names") or C.TOP_N
    if plan["signal"] == "IN" and plan["target_names"] < want * 0.8:
        bad.append(f"only {plan['target_names']} of {want} signal names allocated")
    if plan["nav"] <= 0:
        bad.append("NAV is zero or negative")
    return bad


def stop_breaches(current: dict, entries: dict, marks: dict,
                  stop_pct: float = None) -> list:
    """Holdings trading at or below entry*(1-stop). Checked daily, not monthly —
    this is the operational change the -35% stop introduces."""
    stop_pct = C.STOP_LOSS_PCT if stop_pct is None else stop_pct
    out = []
    for sym, qty in current.items():
        if qty <= 0 or sym not in entries or sym not in marks:
            continue
        entry, mark = float(entries[sym]), float(marks[sym])
        if entry > 0 and mark <= entry * (1 - stop_pct):
            out.append({"symbol": sym, "side": "SELL", "qty": int(qty),
                        "entry": round(entry, 2), "mark": round(mark, 2),
                        "loss_pct": round((mark / entry - 1) * 100, 2),
                        "reason": f"stop-loss {stop_pct*100:.0f}%"})
    return out
