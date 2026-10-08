"""
Fetch NIFTY (or SENSEX) INDEX 1-minute bars from Breeze.

The Breeze option files carry no spot column, and put-call parity recovers the
FORWARD price, not spot -- measured against known spot it runs ~33 points rich
with a 14-point spread, because of cost of carry. So the index is fetched
directly instead: exchange_code="NSE", product_type="cash" returns a full
376-bar session.

Output: data/BREEZE_OPTIONS/{stock_code}/index_1min.parquet
        columns: datetime, open, high, low, close   (deduped on datetime)

    python -m options.breeze.fetch_index_spot --start 2026-06-01 --end 2026-09-12
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse

import pandas as pd

from options.breeze.config import DATA_DIR
from options.breeze.session import get_client
from options.breeze.throttle import Throttle


def out_path(stock_code: str) -> Path:
    return DATA_DIR / stock_code / "index_1min.parquet"


def fetch(start: str, end: str, stock_code: str = "NIFTY",
          exchange_code: str = "NSE", dry_run: bool = False) -> pd.DataFrame:
    days = pd.bdate_range(start, end)
    print(f"{stock_code} index 1-minute | {start} to {end} | {len(days)} weekdays "
          f"| {len(days)} calls (1 per day, 376 bars < 1000 cap)")
    if dry_run:
        return pd.DataFrame()

    client = get_client()
    throttle = Throttle()
    print(f"Budget remaining today: {throttle.remaining():,}")

    frames, empty = [], 0
    for d in days:
        ds = d.strftime("%Y-%m-%d")
        throttle.acquire()
        try:
            r = client.get_historical_data_v2(
                interval="1minute",
                from_date=f"{ds}T09:15:00.000Z", to_date=f"{ds}T15:30:00.000Z",
                stock_code=stock_code, exchange_code=exchange_code, product_type="cash",
            )
        except Exception as exc:
            print(f"  {ds}  EXC {type(exc).__name__}: {str(exc)[:80]}")
            continue
        rows = r.get("Success") or []
        if not rows:
            empty += 1          # holidays land here legitimately
            continue
        frames.append(pd.DataFrame(rows))
        print(f"  {ds}  {len(rows):>4} bars")

    if not frames:
        print("No data returned.")
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = (df[["datetime", "open", "high", "low", "close"]]
          .dropna()
          .drop_duplicates(subset=["datetime"])
          .sort_values("datetime")
          .reset_index(drop=True))

    path = out_path(stock_code)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():                      # merge with anything already fetched
        old = pd.read_parquet(path)
        df = (pd.concat([old, df], ignore_index=True)
              .drop_duplicates(subset=["datetime"])
              .sort_values("datetime")
              .reset_index(drop=True))
    df.to_parquet(path, index=False)

    print(f"\nSaved {len(df):,} rows -> {path}")
    print(f"Range: {df['datetime'].min()} to {df['datetime'].max()} "
          f"| {df['datetime'].dt.date.nunique()} trading days | {empty} empty (holidays)")
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--stock-code", default="NIFTY", dest="stock_code")
    ap.add_argument("--exchange-code", default="NSE", dest="exchange_code")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    fetch(a.start, a.end, a.stock_code, a.exchange_code, a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
