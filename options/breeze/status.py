"""What has actually been downloaded so far.

Reads the parquet partitions and the manifest, so it is safe to run while a
download is in flight (it never touches the API and spends no budget).

    python -m options.breeze.status
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
from collections import Counter
from datetime import date

import pandas as pd

from options.breeze.config import DATA_DIR, MANIFEST_PATH, MAX_CALLS_PER_DAY
from options.breeze.throttle import BUDGET_PATH


def main() -> int:
    print("=" * 72)
    print("BREEZE DATASET STATUS")
    print("=" * 72)

    if not DATA_DIR.exists():
        print(f"  Nothing downloaded yet ({DATA_DIR} does not exist).")
        return 0

    grand_rows = 0
    for stock_dir in sorted(p for p in DATA_DIR.iterdir() if p.is_dir()):
        parquets = sorted(stock_dir.rglob("*.parquet"))
        if not parquets:
            continue
        print(f"\n  {stock_dir.name}")
        for path in parquets:
            try:
                df = pd.read_parquet(path, columns=["datetime", "strike_price",
                                                    "option_type", "expiry"])
            except Exception as exc:
                print(f"    {path.name}: unreadable ({exc})")
                continue
            grand_rows += len(df)
            days = df["datetime"].dt.date.nunique()
            contracts = df[["expiry", "strike_price", "option_type"]].drop_duplicates()
            size_mb = path.stat().st_size / 1e6
            print(f"    {path.relative_to(stock_dir)}")
            print(f"      rows {len(df):>12,}   days {days:>4}   "
                  f"contracts {len(contracts):>5}   {size_mb:>7.1f} MB")
            print(f"      {df['datetime'].min()}  ->  {df['datetime'].max()}")

    print(f"\n  TOTAL ROWS: {grand_rows:,}")

    # ---- manifest ----
    if MANIFEST_PATH.exists():
        try:
            man = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            man = {}
        if man:
            counts = Counter(v.get("status", "?") for v in man.values())
            print(f"\n  Manifest: {len(man):,} contract-days recorded  {dict(counts)}")

            # Where the download has reached, per interval.
            by_interval: dict = {}
            for key in man:
                parts = key.split("|")
                if len(parts) >= 7:
                    interval, trade_date = parts[6], parts[2]
                    lo, hi = by_interval.get(interval, (trade_date, trade_date))
                    by_interval[interval] = (min(lo, trade_date), max(hi, trade_date))
            for interval, (lo, hi) in sorted(by_interval.items()):
                print(f"    {interval:<9} covered {lo} -> {hi}")

    # ---- today's budget ----
    if BUDGET_PATH.exists():
        try:
            b = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
            if b.get("date") == date.today().isoformat():
                used = b.get("used", 0)
                print(f"\n  Calls used today: {used:,} / {MAX_CALLS_PER_DAY:,} "
                      f"({MAX_CALLS_PER_DAY - used:,} nominal remaining)")
        except json.JSONDecodeError:
            pass

    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
