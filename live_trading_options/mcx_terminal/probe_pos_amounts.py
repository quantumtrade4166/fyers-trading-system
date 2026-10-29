"""READ-ONLY: print Kotak P's MCX position rows (amount fields + multipliers) to check how
buyAmt / sellAmt are scaled per product. Places nothing."""
import os
import sys

from live_trading_options.mcx_terminal.kotak_session import login

sys.stdout.reconfigure(encoding="utf-8")
KEYS = ("trdSym", "prod", "flBuyQty", "flSellQty", "buyAmt", "sellAmt", "cfBuyQty", "cfSellQty", "cfBuyAmt",
        "cfSellAmt", "lotSz", "brdLtQty", "multiplier", "prcNum", "prcDen", "genNum", "genDen", "precision")

if __name__ == "__main__":
    s = login()
    r = s.client.positions()
    rows = r.get("data") if isinstance(r, dict) else r
    for x in rows or []:
        if "mcx" in str(x.get("exSeg", "")).lower():
            print({k: x.get(k) for k in KEYS})
    tr = s.client.trade_report()
    trows = tr.get("data") if isinstance(tr, dict) else tr
    for t in (trows or [])[-12:]:
        print("TRADE", {k: t.get(k) for k in ("trdSym", "trnsTp", "fldQty", "avgPrc", "exTm", "nOrdNo")})
    sys.stdout.flush()
    os._exit(0)
