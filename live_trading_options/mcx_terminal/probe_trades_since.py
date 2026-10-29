"""READ-ONLY: Kotak P MCX trades after a given time today (who/what closed positions)."""
import os
import sys

from live_trading_options.mcx_terminal.kotak_session import login

sys.stdout.reconfigure(encoding="utf-8")
SINCE = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else "00:00:00"

if __name__ == "__main__":
    s = login()
    r = s.client.trade_report()
    rows = r.get("data") if isinstance(r, dict) else r
    o = s.client.order_report()
    orows = {x.get("nOrdNo"): x for x in ((o.get("data") if isinstance(o, dict) else o) or [])}
    for t in sorted(rows or [], key=lambda t: str(t.get("exTm"))):
        tm = str(t.get("exTm", ""))[-8:]
        if tm >= SINCE:
            od = orows.get(t.get("nOrdNo"), {})
            print(tm, t.get("trdSym"), t.get("trnsTp"), t.get("fldQty"), "@", t.get("avgPrc"),
                  "type", od.get("prcTp"), "tag", od.get("tag") or od.get("ig") or "(none: placed outside the terminal)")
    sys.stdout.flush()
    os._exit(0)
