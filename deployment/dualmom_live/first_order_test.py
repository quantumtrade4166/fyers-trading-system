"""
GO-LIVE STEP 6 — ONE real 1-share order on Kotak Rohit, before the full basket.

Every part of the order path has been checked against the live API EXCEPT actually
placing an order: resolve, tick, limit pricing, holdings, cash and quotes are all
verified, but place_order, the fill poll, the order report and whether our `tag`
comes back have never run for real, and never from the new source IP. This proves
them with 1 share of IDEA (about Rs 15), a name that is in September's basket
anyway, so the share is not wasted.

PLACES A REAL ORDER. Run only in market hours, supervised:
    .venv\\Scripts\\python.exe -m deployment.dualmom_live.first_order_test --yes
"""

import json
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

SYMBOL = "IDEA"
QTY = 1


def main():
    if "--yes" not in sys.argv:
        print(__doc__)
        print("Refusing without --yes.")
        return 2

    import pytz
    now = datetime.now(pytz.timezone("Asia/Kolkata"))
    if not (now.weekday() < 5 and (9, 20) <= (now.hour, now.minute) <= (15, 15)):
        print(f"IST {now:%a %H:%M} is outside 09:20-15:15 on a weekday. Refusing.")
        return 2

    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    from deployment.dualmom_live import config as C
    from deployment.dualmom_live import kotak_auth_dm as A
    from deployment.dualmom_live import kotak_equity as K
    from deployment.dualmom_live import source_ip as SIP

    c = A.login(verbose=True)
    print(f"source IP pinned: {SIP.installed_ip()}")
    r = K.resolve(c, SYMBOL)
    ltp = K.last_prices(c, [SYMBOL]).get(SYMBOL)
    if not ltp:
        print("no live price — refusing")
        return 1
    limit = K.marketable_limit(ltp, "BUY", C.MARKETABLE_BUFFER, r["tick_size"])
    print(f"{SYMBOL}: {r['trading_symbol']} tick {r['tick_size']} ltp {ltp} -> BUY {QTY} @ limit {limit}")

    res = K.place_limit(c, r["trading_symbol"], "BUY", QTY, limit, tick=r["tick_size"],
                        tag=C.ORDER_TAG)
    print("\n1. place_limit ->", json.dumps({k: v for k, v in res.items() if k != "raw"}, default=str))
    if res["status"] != "PLACED":
        print("   raw:", json.dumps(res.get("raw"), default=str)[:600])
        print("ORDER NOT PLACED — stop here and investigate.")
        return 1

    oid = res["order_id"]
    fill = K.await_fill(c, oid, timeout=120, poll=2)
    print("2. await_fill  ->", json.dumps({k: v for k, v in fill.items() if k != "raw"}, default=str))

    st = K.order_status(c, oid)
    print("3. order_status ->", json.dumps({k: v for k, v in st.items() if k != "raw"}, default=str))

    mine = K.strategy_orders(c, C.ORDER_TAG)
    hit = [o for o in mine if str(o.get("order_id") or o.get("nOrdNo")) == str(oid)]
    print(f"4. TAG ROUND-TRIP: strategy_orders('{C.ORDER_TAG}') returned {len(mine)} order(s); "
          f"ours present: {bool(hit)}")
    try:
        rep = c.order_report()
        rows = rep.get("data") if isinstance(rep, dict) else rep
        row = next((x for x in rows or [] if str(x.get("nOrdNo")) == str(oid)), None)
        if row:
            print("   raw order_report row keys:", sorted(row))
            print("   tag field(s):", {k: row[k] for k in row if "tag" in k.lower()})
    except Exception as e:
        print("   order_report read failed:", type(e).__name__, e)

    try:
        pos = c.positions()
        prow = [p for p in (pos.get("data") or []) if SYMBOL in str(p.get("trdSym", ""))]
        print(f"5. positions: {len(prow)} row(s) for {SYMBOL}  (same-day CNC buys show here, "
              f"not in holdings until settlement)")
    except Exception as e:
        print("5. positions read failed:", type(e).__name__, e)

    ok = fill.get("ok") and bool(hit)
    print("\nSTEP 6", "PASSED" if ok else "NOT PASSED — do not deploy the basket yet")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
