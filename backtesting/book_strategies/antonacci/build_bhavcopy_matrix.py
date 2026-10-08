"""
build_bhavcopy_matrix.py
Turn 5,374 daily NSE bhavcopy files into a survivorship-free, corporate-action
adjusted price matrix plus a turnover matrix.

IMPORTANT CORRECTION
--------------------
An earlier version of this file tried to derive corporate actions from the
bhavcopy itself, assuming NSE adjusts PREVCLOSE. **It does not.** Verified on
TCS 2018-05-31 (1:1 bonus): close 1741.05, prev_close 3514.10 -- the raw prior
close. That approach detected nothing real and made the data worse.

Splits/bonuses now come from NSE's corporate-actions register
(`fetch_corporate_actions.py` -> corporate_actions.csv), which is the authority.

Adjustment: a price on date d is multiplied by the product of the factors of
every corporate action with ex_date > d, so the series is back-adjusted onto
today's share basis.

Outputs (Bhavcopy/_matrix/):
  close_adj.parquet   back-adjusted close,  date x symbol
  close_raw.parquet   unadjusted close,     date x symbol
  value.parquet       traded value (Rs),    date x symbol
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import glob
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(r"G:\fyers_data_pipeline")
BHAV = ROOT / "Bhavcopy"
OUT  = BHAV / "_matrix"
CA   = OUT / "corporate_actions.csv"

MIN_DAYS = 60          # ignore symbols with almost no history


def load_long():
    # only the year folders -- must not pick up our own output in _matrix/
    files = sorted(f for f in glob.glob(str(BHAV / "*" / "*.parquet"))
                   if Path(f).parent.name.isdigit())
    print(f"reading {len(files)} bhavcopy files ...")
    parts, t0 = [], time.time()
    for n, f in enumerate(files, 1):
        d = pd.read_parquet(f, columns=["symbol", "close", "value"])
        d["date"] = pd.Timestamp(Path(f).stem)
        parts.append(d)
        if n % 2000 == 0:
            print(f"  {n}/{len(files)}  {time.time()-t0:.0f}s", flush=True)
    df = pd.concat(parts, ignore_index=True)
    print(f"  long frame: {len(df):,} rows, {df['symbol'].nunique():,} symbols")
    return df


def apply_actions(close: pd.DataFrame) -> pd.DataFrame:
    """Back-adjust each column using the NSE corporate-actions register."""
    ca = pd.read_csv(CA, parse_dates=["ex_date"])
    ca = ca[ca["symbol"].isin(close.columns)]
    print(f"  applying {len(ca):,} actions over {ca['symbol'].nunique():,} symbols")

    adj = close.copy()
    dates = close.index.values
    applied = 0
    for sym, grp in ca.groupby("symbol", sort=False):
        col = adj[sym].to_numpy(copy=True)
        for ex, f in zip(grp["ex_date"].values, grp["factor"].values):
            mask = dates < ex
            if mask.any():
                col[mask] = col[mask] * f
                applied += 1
        adj[sym] = col
    print(f"  factors applied: {applied:,}")
    return adj


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if not CA.exists():
        sys.exit("corporate_actions.csv missing -- run fetch_corporate_actions.py first")
    t0 = time.time()

    df = load_long()
    counts = df.groupby("symbol")["date"].size()
    df = df[df["symbol"].isin(counts[counts >= MIN_DAYS].index)]
    print(f"  after >={MIN_DAYS}-day filter: {df['symbol'].nunique():,} symbols")

    print("pivoting ...")
    close = df.pivot(index="date", columns="symbol", values="close").astype("float64")
    value = df.pivot(index="date", columns="symbol", values="value").astype("float32")
    print(f"  matrix {close.shape[0]} days x {close.shape[1]} symbols")

    adj = apply_actions(close)

    close.astype("float32").to_parquet(OUT / "close_raw.parquet")
    adj.astype("float32").to_parquet(OUT / "close_adj.parquet")
    value.to_parquet(OUT / "value.parquet")

    print(f"\ndone in {(time.time()-t0)/60:.1f} min -> {OUT}")
    print(f"  range {close.index[0].date()} -> {close.index[-1].date()}")

    for m, lbl in ((close, "raw"), (adj, "adjusted")):
        r = m / m.shift(1)
        hits = int(((r < 0.62) & (r > 0.03)).sum().sum())
        print(f"  suspected fake crashes ({lbl}): {hits:,}")


if __name__ == "__main__":
    main()
