"""Reconstruct a NIFTY spot price series from the options chain, 2021-2026.

Why this exists: the only NIFTY index bars on disk
(data/NSE_NIFTY50_INDEX/{year}/ohlcv_5min.parquet) start 2024-05-28 — about
2.1 years. That is too short to say anything trustworthy about daily or 4-hour
systems, where a couple of years is a handful of independent trend cycles.

The options dataset carries a `spot` column on every row, 2021-2026. Collapsing
it to one price per minute recovers ~5.5 years of index history for free.

**The catch, stated plainly:** `spot` is a snapshot, one number per minute, not
a true 1-minute OHLC bar. Bars resampled from it therefore have slightly TIGHT
highs and lows — the true intrabar extreme between two snapshots is invisible.
Open and close are exact. This matters for anything triggering on wicks
(breakout stops, intrabar stop-losses), so the reconciliation below measures
the size of the error against real index bars rather than assuming it is small.

    python -m backtesting.book_strategies.kaufman.build_nifty_spot
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OPTIONS_DIR = PROJECT_ROOT / "data" / "NSE_NIFTY_OPTIONS"
INDEX_DIR = PROJECT_ROOT / "data" / "NSE_NIFTY50_INDEX"
OUT_PATH = INDEX_DIR / "derived_spot_1min.parquet"


def build() -> pd.DataFrame:
    """One spot price per minute, across every year on disk."""
    frames = []
    for parquet in sorted(OPTIONS_DIR.glob("[0-9][0-9][0-9][0-9]/ohlcv_1min.parquet")):
        df = pd.read_parquet(parquet, columns=["datetime", "spot"])
        df = df.dropna(subset=["spot"])
        # Every contract on the chain repeats the same spot for a given minute,
        # so any one of them *should* do. Use the median instead of the first:
        # on 2025-08-29 10:04 roughly half the 42 rows carried 12,484.35 against
        # a true 24,473.65, and taking the first row picked the corrupt one —
        # a 49% error on that bar. The median ignores a minority of bad rows.
        minute = df.groupby("datetime")["spot"].median()
        frames.append(minute)
        print(f"  {parquet.parent.name}: {len(df):>10,} rows -> {len(minute):>7,} minutes")

    if not frames:
        raise SystemExit(f"No options parquets under {OPTIONS_DIR}")

    spot = pd.concat(frames).sort_index()
    spot = spot[~spot.index.duplicated(keep="first")]

    out = pd.DataFrame({
        "open": spot, "high": spot, "low": spot, "close": spot,
    })
    out.index.name = "datetime"
    out.to_parquet(OUT_PATH)

    print(f"\n✅ {len(out):,} minutes  {out.index.min()} → {out.index.max()}")
    print(f"   {OUT_PATH}")
    return out


def reconcile(derived: pd.DataFrame) -> None:
    """Measure the derived series against real index bars on their overlap.

    This is the gate. If the derived series disagrees with real bars on the
    period where both exist, nothing built on the earlier years can be trusted.
    """
    real_files = sorted(INDEX_DIR.glob("[0-9][0-9][0-9][0-9]/ohlcv_5min.parquet"))
    if not real_files:
        print("\n⚠ No real index bars to reconcile against — skipping the check.")
        return

    real = pd.concat([pd.read_parquet(f) for f in real_files], ignore_index=True)
    real = real.set_index("datetime").sort_index()

    # Resample the derived minutes onto the same 5-minute grid.
    d5 = derived.resample("5min", closed="left", label="left").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).dropna(subset=["open", "close"])

    both = real.join(d5, how="inner", lsuffix="_real", rsuffix="_der")
    if both.empty:
        print("\n⚠ No overlapping bars — cannot reconcile.")
        return

    print(f"\n--- Reconciliation on {len(both):,} overlapping 5-min bars "
          f"({both.index.min().date()} → {both.index.max().date()}) ---")

    rows = []
    for field in ("open", "high", "low", "close"):
        a = both[f"{field}_real"].to_numpy(float)
        b = both[f"{field}_der"].to_numpy(float)
        diff = b - a
        bps = np.abs(diff) / a * 10_000
        rows.append({
            "field": field,
            "mean_bps": round(float(np.mean(bps)), 3),
            "p95_bps": round(float(np.percentile(bps, 95)), 3),
            "max_bps": round(float(np.max(bps)), 2),
            "corr": round(float(np.corrcoef(a, b)[0, 1]), 6),
        })
    table = pd.DataFrame(rows)
    print(table.to_string(index=False))

    hi = table.loc[table["field"] == "high", "mean_bps"].item()
    lo = table.loc[table["field"] == "low", "mean_bps"].item()
    cl = table.loc[table["field"] == "close", "mean_bps"].item()

    print(f"\nClose error {cl:.3f} bps — open/close are snapshots of the same "
          f"instant, so this is the honest accuracy floor.")
    print(f"High/low error {hi:.3f}/{lo:.3f} bps — the tight-wick effect. "
          f"Bars are built from minute snapshots, so true intrabar extremes "
          f"between snapshots are missed.")
    if max(hi, lo) > 5:
        print("⚠ Wick error above 5bps: do NOT use this series for strategies "
              "that trigger on highs/lows (breakouts, intrabar stops).")
    else:
        print("✅ Wick error small enough for daily/4h work; still prefer real "
              "bars where they exist.")


if __name__ == "__main__":
    reconcile(build())
