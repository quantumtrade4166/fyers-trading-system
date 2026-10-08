"""
Discover and append NIFTY weekly expiry dates by probing Breeze.

The local calendar stops at 2026-05-19, so the downloader plans zero contracts
for anything later. NIFTY has expired on TUESDAYS since 2025-09-02, but a
holiday shifts the expiry earlier, so candidates are probed rather than assumed:
Tuesday first, then Monday, then Wednesday.

A candidate is confirmed when a near-ATM contract for that expiry returns bars.
Confirmed dates are appended to data/NSE_NIFTY_OPTIONS/expiry_calendar.csv
(a .bak copy is written first). Costs ~1-3 calls per candidate week.

    python -m options.breeze.discover_expiries --start 2026-06-01 --end 2026-09-30
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import shutil

import pandas as pd

from options.breeze.config import DATA_DIR, PROJECT_ROOT
from options.breeze.session import get_client
from options.breeze.throttle import Throttle

CALENDAR = PROJECT_ROOT / "data" / "NSE_NIFTY_OPTIONS" / "expiry_calendar.csv"
INDEX_PATH = DATA_DIR / "NIFTY" / "index_1min.parquet"
OFFSETS = [0, -1, 1]          # Tuesday, then Monday, then Wednesday


def _iso(d):
    return pd.Timestamp(d).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def probe(client, throttle, expiry, strike, spot_day):
    """True if a near-ATM contract for this expiry returns any bars."""
    for right in ("call", "put"):
        throttle.acquire()
        try:
            r = client.get_historical_data_v2(
                interval="1minute",
                from_date=_iso(pd.Timestamp(spot_day).replace(hour=9, minute=15)),
                to_date=_iso(pd.Timestamp(spot_day).replace(hour=15, minute=30)),
                stock_code="NIFTY", exchange_code="NFO", product_type="options",
                expiry_date=_iso(pd.Timestamp(expiry).replace(hour=7)),
                right=right, strike_price=str(int(strike)),
            )
        except Exception as exc:
            print(f"      {right} EXC {type(exc).__name__}: {str(exc)[:60]}")
            continue
        if r.get("Status") == 200 and (r.get("Success") or []):
            return True, len(r["Success"])
    return False, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    idx = pd.read_parquet(INDEX_PATH)
    idx["d"] = idx["datetime"].dt.normalize()
    daily_spot = idx.groupby("d")["close"].last()
    trading_days = list(daily_spot.index)

    tuesdays = pd.date_range(a.start, a.end, freq="W-TUE")
    print(f"Candidate weeks: {len(tuesdays)} ({a.start} to {a.end})")

    existing = set()
    if CALENDAR.exists():
        existing = set(pd.read_csv(CALENDAR)["expiry_date"].astype(str))

    if a.dry_run:
        print("DRY RUN — candidates:", [t.strftime('%Y-%m-%d') for t in tuesdays])
        return 0

    client, throttle = get_client(), Throttle()
    print(f"Budget remaining: {throttle.remaining():,}\n")

    found = []
    for tue in tuesdays:
        if tue.strftime("%Y-%m-%d") in existing:
            print(f"{tue.date()}  already in calendar")
            continue
        hit = False
        for off in OFFSETS:
            cand = tue + pd.Timedelta(days=off)
            if cand not in trading_days:
                continue
            # probe on a day a few sessions before expiry, using that day's spot
            prior = [d for d in trading_days if d < cand]
            if not prior:
                continue
            probe_day = prior[max(0, len(prior) - 3)]
            strike = round(daily_spot.loc[probe_day] / 50) * 50
            ok, n = probe(client, throttle, cand, strike, probe_day)
            if ok:
                print(f"{cand.date()}  CONFIRMED ({cand.strftime('%a')}, strike {int(strike)}, {n} bars)")
                found.append(cand.strftime("%Y-%m-%d"))
                hit = True
                break
        if not hit:
            print(f"{tue.date()}  no contract found on Tue/Mon/Wed")

    if not found:
        print("\nNothing new to add.")
        return 0

    shutil.copy(CALENDAR, CALENDAR.with_suffix(".csv.bak"))
    cal = pd.read_csv(CALENDAR)
    cal = pd.concat([cal, pd.DataFrame({"expiry_date": found})], ignore_index=True)
    cal["expiry_date"] = cal["expiry_date"].astype(str)
    cal = cal.drop_duplicates("expiry_date").sort_values("expiry_date").reset_index(drop=True)
    cal.to_csv(CALENDAR, index=False)
    print(f"\nAdded {len(found)} expiries -> {CALENDAR} (backup: {CALENDAR.name}.bak)")
    print(f"Calendar now {len(cal)} dates, last: {list(cal['expiry_date'].tail(4))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
