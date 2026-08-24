"""Determine whether SENSEX (BFO) options are available on Breeze.

The docs contradict each other — the API reference says "securities listed on BSE
and MCX are not available", while the SDK ships BFO examples. And we have no
local SENSEX data to build a known-good probe target from, unlike NIFTY.

So: get spot from Breeze itself, derive ATM, then sweep recent weekdays as
candidate expiries. BSE has moved SENSEX weekly expiry more than once
(Friday -> Tuesday -> Thursday), so the expiry weekday is discovered, not assumed.

    python -m options.breeze.probe_sensex
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
from datetime import date, datetime, timedelta

import pandas as pd

from options.breeze.config import PROBE_DIR
from options.breeze.session import get_client
from options.breeze.throttle import DailyBudgetExhausted, Throttle

FMT = "%Y-%m-%dT%H:%M:%S.000Z"
STRIKE_STEP = 100


def main() -> int:
    client = get_client()
    throttle = Throttle(verbose=True)
    results = {"run_at": datetime.now().isoformat(timespec="seconds")}

    # ---- 1. Is BSESEN reachable at all? Get spot to anchor the strike. ----
    print("=" * 70)
    print("1. SENSEX spot via Breeze (anchors the ATM strike)")
    print("=" * 70)

    spot = None
    for exchange, code in (("BSE", "BSESEN"), ("BSE", "SENSEX")):
        throttle.acquire()
        try:
            resp = client.get_quotes(stock_code=code, exchange_code=exchange,
                                     product_type="cash")
        except Exception as exc:
            print(f"  [FAIL] {exchange}/{code}: {type(exc).__name__}: {exc}")
            continue
        success = resp.get("Success") or []
        print(f"  [{'OK  ' if success else 'FAIL'}] {exchange}/{code}: "
              f"Status={resp.get('Status')} Error={resp.get('Error')}")
        if success:
            ltp = success[0].get("ltp") or success[0].get("last")
            print(f"         spot = {ltp}")
            if ltp:
                spot = float(ltp)
                results["spot"] = {"code": code, "ltp": ltp}
                break

    if spot is None:
        # Fall back to a plausible level; the expiry sweep tries several strikes.
        spot = 82000.0
        print(f"  No spot available — falling back to {spot:.0f} and sweeping strikes.")
        results["spot"] = {"fallback": spot}

    atm = int(round(spot / STRIKE_STEP) * STRIKE_STEP)

    # ---- 2. Sweep recent weekdays as candidate expiries ----
    print("\n" + "=" * 70)
    print(f"2. Expiry sweep (ATM {atm}, +/- a few strikes)")
    print("=" * 70)

    today = date.today()
    candidates = []
    d = today - timedelta(days=3)
    while d > today - timedelta(days=40):
        if d.weekday() < 5:
            candidates.append(d)
        d -= timedelta(days=1)

    strikes = [atm, atm + 100, atm - 100, atm + 500, atm - 500]
    attempts, found = [], None

    try:
        for exp in candidates:
            # Trade day = previous weekday (contract must exist before expiry).
            trade = exp - timedelta(days=1)
            while trade.weekday() >= 5:
                trade -= timedelta(days=1)
            day = pd.Timestamp(trade)

            hit = False
            for strike in strikes:
                throttle.acquire()
                try:
                    resp = client.get_historical_data_v2(
                        interval="1minute",
                        from_date=day.replace(hour=9, minute=20).strftime(FMT),
                        to_date=day.replace(hour=9, minute=40).strftime(FMT),
                        stock_code="BSESEN", exchange_code="BFO",
                        product_type="options",
                        expiry_date=pd.Timestamp(exp).strftime("%Y-%m-%dT07:00:00.000Z"),
                        right="call", strike_price=str(strike),
                    )
                except Exception as exc:
                    attempts.append({"expiry": str(exp), "strike": strike,
                                     "error": f"{type(exc).__name__}: {exc}"})
                    continue

                rows = len(resp.get("Success") or [])
                attempts.append({"expiry": str(exp), "strike": strike,
                                 "status": resp.get("Status"),
                                 "error": resp.get("Error"), "rows": rows})
                if rows:
                    print(f"  [OK  ] expiry {exp} ({pd.Timestamp(exp).day_name()[:3]}) "
                          f"{strike}CE on {trade}: {rows} rows")
                    found = attempts[-1]
                    hit = True
                    break

            if hit:
                break
            print(f"  [    ] expiry {exp} ({pd.Timestamp(exp).day_name()[:3]}): no data "
                  f"(last error: {attempts[-1].get('error')})")
    except DailyBudgetExhausted as exc:
        print(f"\n  {exc}")

    results["attempts"] = attempts
    results["works"] = bool(found)
    results["working_example"] = found

    # ---- 3. If it works, check 1-second too ----
    if found:
        print("\n" + "=" * 70)
        print("3. 1-SECOND data for SENSEX")
        print("=" * 70)
        exp = found["expiry"]
        trade = pd.Timestamp(exp) - pd.Timedelta(days=1)
        while trade.weekday() >= 5:
            trade -= pd.Timedelta(days=1)
        throttle.acquire()
        resp = client.get_historical_data_v2(
            interval="1second",
            from_date=trade.replace(hour=9, minute=20).strftime(FMT),
            to_date=trade.replace(hour=9, minute=30).strftime(FMT),
            stock_code="BSESEN", exchange_code="BFO", product_type="options",
            expiry_date=pd.Timestamp(exp).strftime("%Y-%m-%dT07:00:00.000Z"),
            right="call", strike_price=str(found["strike"]),
        )
        rows = len(resp.get("Success") or [])
        print(f"  1second: rows={rows} Status={resp.get('Status')} "
              f"Error={resp.get('Error')}")
        results["one_second"] = {"rows": rows, "status": resp.get("Status"),
                                 "error": resp.get("Error")}

    print("\n" + "=" * 70)
    if results["works"]:
        print("  SENSEX / BFO : WORKS")
        print(f"  Example      : {found}")
        if results.get("one_second", {}).get("rows"):
            print("  1-second     : available")
        else:
            print("  1-second     : NOT available (1-minute only)")
    else:
        print("  SENSEX / BFO : NOT AVAILABLE on this account/API")
        print("  -> NIFTY only. SENSEX would need another source.")
    print(f"  Calls used   : {throttle.used}  (left today {throttle.remaining():,})")
    print("=" * 70)

    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    out = PROBE_DIR / "sensex_probe.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"  Saved        : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
