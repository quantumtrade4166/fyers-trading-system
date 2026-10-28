"""
mcx_terminal/probe_broker_state.py — READ-ONLY: Kotak P's MCX positions + today's MCX orders, side by side
with the terminal's live book file. Sends nothing.

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.probe_broker_state
"""
import datetime as dt
import json
import sys

from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal.kotak_session import login

sys.stdout.reconfigure(encoding="utf-8")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def rows(r):
    d = r.get("data") if isinstance(r, dict) else r
    return [x for x in (d or []) if isinstance(x, dict) and "mcx" in str(x.get("exSeg", "")).lower()]


def main():
    s = login()
    c = s.client
    print("=== Kotak P MCX positions ===")
    pos = rows(c.positions())
    for p in pos:
        keys = ("trdSym", "prod", "flBuyQty", "flSellQty", "cfBuyQty", "cfSellQty", "buyAmt", "sellAmt",
                "cfBuyAmt", "cfSellAmt", "lotSz", "multiplier", "genNum", "genDen", "prcNum", "prcDen", "tok", "stkPrc")
        print({k: p.get(k) for k in keys if k in p})
    if pos:
        print("all position keys:", sorted(pos[0].keys()))
    print("=== Kotak P MCX orders today ===")
    for o in rows(c.order_report()):
        print({k: o.get(k) for k in ("nOrdNo", "ordDtTm", "trdSym", "trnsTp", "qty", "fldQty", "avgPrc", "prc",
                                     "ordSt", "prod", "rejRsn") if k in o}, "tag", o.get("tag") or o.get("ig") or o.get("ordTag"))
    for f in sorted(C.DATA.glob("live_book_*.json"))[-3:]:
        d = json.loads(f.read_text(encoding="utf-8"))
        print(f"=== {f.name}: {len(d.get('orders', []))} orders ===")
        for t, p in d.get("positions", {}).items():
            if p.get("net"):
                print("  open", p.get("symbol"), p.get("net"), "avg", p.get("avg"), "carried" if p.get("carried") else "")


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stdout.flush()
        import os
        os._exit(0)
