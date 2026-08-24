"""Cross-check Breeze data against the existing (trusted) Fyers dataset.

Before any strategy is built on Breeze data, it has to agree with the data
already validated against iCharts. This resamples Breeze 1-second candles up to
1-minute and compares OHLCV against `data/NSE_NIFTY_OPTIONS/` for the same
contract-minutes.

Costs no API budget — both datasets are local.

    python -m options.breeze.validate_against_fyers
    python -m options.breeze.validate_against_fyers --tolerance 0.05
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse

import pandas as pd

from options.breeze.config import DATA_DIR, PROJECT_ROOT

FYERS_DIR = PROJECT_ROOT / "data" / "NSE_NIFTY_OPTIONS"


def load_breeze() -> pd.DataFrame:
    frames = []
    for path in sorted((DATA_DIR / "NIFTY").rglob("ohlcv_1second.parquet")):
        frames.append(pd.read_parquet(path))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def resample_to_1min(df: pd.DataFrame) -> pd.DataFrame:
    """1-second -> 1-minute OHLCV per contract."""
    df = df.copy()
    df["minute"] = df["datetime"].dt.floor("min")
    grouped = df.groupby(["minute", "expiry", "strike_price", "option_type"],
                         observed=True)
    out = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        ticks=("close", "size"),
    ).reset_index()
    return out.rename(columns={"minute": "datetime"})


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare Breeze vs Fyers data")
    ap.add_argument("--tolerance", type=float, default=0.01,
                    help="relative tolerance for price agreement (default 1%%)")
    args = ap.parse_args()

    breeze = load_breeze()
    if breeze.empty:
        print("No Breeze 1-second data downloaded yet.")
        return 0

    days = sorted(breeze["datetime"].dt.date.unique())
    print("=" * 72)
    print(f"Breeze 1-second: {len(breeze):,} rows, {len(days)} day(s) "
          f"({days[0]} -> {days[-1]})")

    b1m = resample_to_1min(breeze)
    print(f"Resampled to 1-minute: {len(b1m):,} contract-minutes")

    # Load the matching Fyers year files.
    years = sorted({d.year for d in days})
    fy_frames = []
    for year in years:
        path = FYERS_DIR / str(year) / "ohlcv_1min.parquet"
        if path.exists():
            fy_frames.append(pd.read_parquet(path))
    if not fy_frames:
        print("No overlapping Fyers data to compare against.")
        return 0

    fyers = pd.concat(fy_frames, ignore_index=True)
    fyers = fyers[fyers["datetime"].dt.date.isin(days)]
    if fyers.empty:
        print("No overlapping Fyers rows for these days.")
        return 0

    # The Fyers dataset is FRONT-WEEK ONLY and carries no expiry column. Matching
    # on (datetime, strike, type) alone would silently compare a next-week Breeze
    # contract against a front-week Fyers one — the next-week contract holds more
    # time value, so it reads as a large price disagreement that is really an
    # apples-to-oranges join. So restrict Breeze to the front expiry per day.
    b = b1m.copy()
    b["option_type"] = b["option_type"].astype(str)
    b["trade_date"] = b["datetime"].dt.date
    b["expiry_date"] = pd.to_datetime(b["expiry"]).dt.date

    front = (b.groupby("trade_date")["expiry_date"].min()
              .rename("front_expiry").reset_index())
    b = b.merge(front, on="trade_date")
    dropped = (b["expiry_date"] != b["front_expiry"]).sum()
    b_single = b[b["expiry_date"] == b["front_expiry"]].copy()
    print(f"Restricted to front-week expiry (Fyers coverage): "
          f"dropped {dropped:,} next-week contract-minutes")

    # Anything still duplicated cannot be attributed unambiguously.
    dupes = b_single.duplicated(["datetime", "strike_price", "option_type"],
                                keep=False)
    b_single = b_single[~dupes]

    f = fyers.copy()
    f["option_type"] = f["option_type"].astype(str)
    f = f[["datetime", "strike_price", "option_type", "open", "high", "low",
           "close", "volume"]]

    merged = b_single.merge(f, on=["datetime", "strike_price", "option_type"],
                            suffixes=("_bz", "_fy"), how="inner")
    print(f"Overlapping contract-minutes: {len(merged):,}")
    if merged.empty:
        print("\nNothing overlaps yet — Breeze has only covered days/contracts "
              "the Fyers set doesn't contain (e.g. next-week expiries).")
        return 0

    print("\n" + "=" * 72)
    print("AGREEMENT")
    print("=" * 72)
    for col in ("open", "high", "low", "close"):
        bz, fy = merged[f"{col}_bz"], merged[f"{col}_fy"]
        denom = fy.abs().where(fy.abs() > 0, 1.0)
        rel = (bz - fy).abs() / denom
        within = (rel <= args.tolerance).mean() * 100
        print(f"  {col:<6} match within {args.tolerance:.1%}: {within:6.2f}%   "
              f"median abs diff {(bz - fy).abs().median():.4f}   "
              f"max {(bz - fy).abs().max():.2f}")

    vb, vf = merged["volume_bz"], merged["volume_fy"]
    exact = (vb == vf).mean() * 100
    print(f"  volume exact match: {exact:6.2f}%   "
          f"median diff {(vb - vf).abs().median():,.0f}")

    worst = merged.assign(d=(merged["close_bz"] - merged["close_fy"]).abs()) \
                  .nlargest(5, "d")
    print("\n  Largest close disagreements:")
    for r in worst.itertuples():
        print(f"    {r.datetime} {r.strike_price}{str(r.option_type)[0]}  "
              f"breeze={r.close_bz:.2f}  fyers={r.close_fy:.2f}  "
              f"diff={r.d:.2f}  ticks={r.ticks}")

    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
