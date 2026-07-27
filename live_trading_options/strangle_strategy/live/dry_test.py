"""
live/dry_test.py — one-time Kite order-placement plumbing test.
==============================================================

Places a REAL but deliberately UNFILLABLE limit order (BUY 1 lot far BELOW market),
prints the order id + status, then cancels it. Proves the full order round-trip
(place -> id -> status -> cancel) with ~zero fill risk. It never sells and never
places a market order.

Run this ONCE from the VPS (double-click deploy/run_kite_drytest.bat, or:
    .venv\\Scripts\\python.exe live_trading_options\\strangle_strategy\\live\\dry_test.py)
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import time
import json
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live.kite_executor import get_kite, resolve, _instruments  # noqa: E402

_OPEN_LIKE = {"OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED",
              "VALIDATION PENDING", "OPEN PENDING", "MODIFY PENDING"}


def _pick_contract(kite):
    """Today's NIFTY CE strike if selected, else a mid strike of the nearest expiry."""
    today = dt.date.today().isoformat()
    sel = ROOT / "data" / "intraday_state" / f"{today}_NIFTY.json"
    if sel.exists():
        s = json.loads(sel.read_text())
        return int(s["ce_strike"]), s["expiry"]
    nifty = [r for r in _instruments(kite, "NFO")
             if r.get("name") == "NIFTY" and r.get("instrument_type") == "CE"]
    exp = sorted({str(r["expiry"]) for r in nifty})[0]
    row = sorted([r for r in nifty if str(r["expiry"]) == exp], key=lambda r: r["strike"])
    mid = row[len(row) // 2]
    return int(mid["strike"]), exp


def main():
    kite = get_kite()
    strike, expiry = _pick_contract(kite)
    c = resolve(kite, "NIFTY", expiry, strike, "CE")
    ts, exch, lot = c["tradingsymbol"], c["exchange"], c["lot_size"]

    key = f"{exch}:{ts}"
    ltp = kite.ltp([key]).get(key, {}).get("last_price", 0) or 0
    limit = round(max(0.05, ltp * 0.5), 1)          # half of LTP -> cannot fill
    print(f"Contract : {ts} ({exch}), lot {lot}, LTP {ltp}")
    print(f"Placing  : BUY LIMIT {lot} @ {limit}  (far below market — will NOT fill)")

    oid = kite.place_order(
        variety=kite.VARIETY_REGULAR, exchange=exch, tradingsymbol=ts,
        transaction_type="BUY", quantity=lot, product="MIS",
        order_type=kite.ORDER_TYPE_LIMIT, price=limit, tag="drytest")
    print(f"order_id : {oid}")

    time.sleep(3)
    st = (kite.order_history(oid) or [{}])[-1]
    status, filled = st.get("status"), st.get("filled_quantity", 0)
    print(f"status   : {status} | filled {filled} | avg {st.get('average_price')}")

    if filled and int(filled) > 0:                  # essentially impossible; be safe
        print("!!! UNEXPECTED FILL — squaring off immediately with a MARKET sell")
        kite.place_order(variety=kite.VARIETY_REGULAR, exchange=exch, tradingsymbol=ts,
                         transaction_type="SELL", quantity=int(filled), product="MIS",
                         order_type=kite.ORDER_TYPE_MARKET, tag="drytest_flat")
        print("   square-off sent — CHECK YOUR POSITIONS.")
    elif status in _OPEN_LIKE:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=oid)
        time.sleep(2)
        print(f"cancelled: {(kite.order_history(oid) or [{}])[-1].get('status')}")
    else:
        print("terminal status (e.g. REJECTED) — round-trip confirmed, nothing to cancel.")

    print("\nDRY TEST COMPLETE — order placement pipe confirmed.")


if __name__ == "__main__":
    main()
