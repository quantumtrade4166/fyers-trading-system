"""READ-ONLY: list every MCX order on Kotak P that is still working (open / trigger pending), and the
MCX positions. Places and cancels nothing."""
import os
import sys

from live_trading_options.mcx_terminal.kotak_session import login

sys.stdout.reconfigure(encoding="utf-8")
DONE = ("complete", "traded", "rejected", "cancelled")

if __name__ == "__main__":
    s = login()
    r = s.client.order_report()
    rows = r.get("data") if isinstance(r, dict) else r
    work = [x for x in rows or [] if "mcx" in str(x.get("exSeg", "")).lower()
            and not any(w in str(x.get("ordSt", "")).lower() for w in DONE)]
    print(f"WORKING MCX ORDERS: {len(work)}")
    for x in work:
        print(" ", x.get("nOrdNo"), x.get("trdSym"), x.get("trnsTp"), x.get("qty"), x.get("ordSt"),
              "trg", x.get("trgPrc"), "px", x.get("prc"), "tag", x.get("tag") or x.get("ig"))
    p = s.client.positions()
    prow = p.get("data") if isinstance(p, dict) else p
    for x in prow or []:
        if "mcx" in str(x.get("exSeg", "")).lower():
            net = sum(float(x.get(k) or 0) for k in ("cfBuyQty", "flBuyQty")) - sum(float(x.get(k) or 0) for k in ("cfSellQty", "flSellQty"))
            if net:
                print("OPEN POSITION", x.get("trdSym"), net)
    sys.stdout.flush()
    os._exit(0)
