"""
mcx_terminal/probe_qty_units.py — does Kotak want MCX order quantity in UNITS or LOTS?

READ-ONLY (sends NO order). Asks Kotak's margin calculator for a CRUDEOIL near-ATM option at
quantity = 1 and quantity = lot size, BUY and SELL, and lists any MCX rows already in today's
Kotak P order book / positions with their quantity fields next to the lot size.

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.probe_qty_units
"""
import json
import sys

from live_trading_options.mcx_terminal import instruments as I
from live_trading_options.mcx_terminal.kotak_session import login

sys.stdout.reconfigure(encoding="utf-8")


def short(x, n=600):
    s = json.dumps(x, default=str) if not isinstance(x, str) else x
    return s[:n]


def main():
    s = login()
    c = s.client
    u = I.load(c)
    p = u.products["CRUDEOIL"]
    exp = p.expiries()[0]
    fut = p.underlying(exp)
    q = c.quotes(instrument_tokens=[{"instrument_token": str(fut.token), "exchange_segment": "mcx_fo"}], quote_type="ltp")
    row = q[0] if isinstance(q, list) else q["message"][0]
    ltp = float(row["ltp"])
    ks = sorted(p.chains[exp])
    k = min(ks, key=lambda x: abs(x - ltp))
    ce = p.chains[exp][k]["CE"]
    print(f"CRUDEOIL fut {fut.symbol} ltp {ltp} · option {ce.symbol} token {ce.token} lot {ce.lot} freeze {ce.freeze}")
    qo = c.quotes(instrument_tokens=[{"instrument_token": str(ce.token), "exchange_segment": "mcx_fo"}], quote_type="ltp")
    oltp = float((qo[0] if isinstance(qo, list) else qo["message"][0])["ltp"])
    print(f"option ltp {oltp}  ->  1 lot premium = {oltp * ce.lot:,.0f} rupees")
    for side in ("B", "S"):
        for qty in (1, ce.lot):
            r = c.margin_required(exchange_segment="mcx_fo", price=str(oltp), order_type="L", product="NRML",
                                  quantity=str(qty), instrument_token=str(ce.token), transaction_type=side)
            print(f"margin_required {side} qty={qty}: {short(r, 400)}")
    for name, fn in (("order_report", c.order_report), ("positions", c.positions)):
        try:
            r = fn()
        except Exception as e:
            print(name, "error", e)
            continue
        rows = r.get("data") if isinstance(r, dict) else r
        rows = [x for x in (rows or []) if isinstance(x, dict) and "mcx" in str(x.get("exSeg", x.get("exchangeSegment", ""))).lower()]
        print(f"{name}: {len(rows)} MCX row(s)")
        for x in rows[:6]:
            keep = {kk: x.get(kk) for kk in ("trdSym", "trnsTp", "qty", "fldQty", "flBuyQty", "flSellQty", "cfBuyQty",
                                              "cfSellQty", "lotSz", "brdLtQty", "multiplier", "genNum", "genDen",
                                              "prcNum", "prcDen", "ordSt", "prod", "avgPrc", "prc") if kk in x}
            print("  ", keep)


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stdout.flush()
        import os
        os._exit(0)
