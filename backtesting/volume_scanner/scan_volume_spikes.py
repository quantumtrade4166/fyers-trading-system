import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
from pathlib import Path
import argparse

DATA_DIR   = Path("G:/fyers_data_pipeline/data")
OUT_DIR    = Path(__file__).parent / "results"
OUT_DIR.mkdir(exist_ok=True)

MULTIPLIER  = 100   # spike must be > N × avg volume in prior window
WINDOW_BARS = 1650  # 30 calendar days ≈ 22 trading days × 75 bars/day


def load_symbol(sym_dir: Path) -> pd.DataFrame | None:
    frames = []
    for year_dir in sorted(sym_dir.iterdir()):
        f = year_dir / "ohlcv_5min.parquet"
        if f.exists():
            frames.append(pd.read_parquet(f))
    if not frames:
        return None
    df = pd.concat(frames)
    if "datetime" in df.columns:
        df = df.set_index("datetime")
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df


def scan_symbol(sym: str, df: pd.DataFrame, multiplier: int) -> pd.DataFrame:
    vol = df["volume"].copy()

    # rolling average of the PRIOR window (exclude current bar via shift(1))
    prior_avg = vol.shift(1).rolling(WINDOW_BARS, min_periods=50).mean()

    # spike condition: current bar > N × 30-day average bar volume
    ratio = vol / prior_avg.replace(0, pd.NA)
    mask  = ratio > multiplier

    hits = df[mask].copy()
    hits["symbol"]         = sym.replace("NSE_", "").replace("_EQ", "")
    hits["volume_spike"]   = vol[mask]
    hits["avg_vol_30d"]    = prior_avg[mask].round(0).astype("Int64")
    hits["ratio"]          = ratio[mask].round(1)

    return hits[["symbol", "volume_spike", "avg_vol_30d", "ratio",
                 "open", "high", "low", "close"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--multiplier", type=int, default=MULTIPLIER,
                        help="Volume spike multiplier (default 10)")
    args = parser.parse_args()

    sym_dirs = sorted([d for d in DATA_DIR.iterdir()
                       if d.is_dir() and d.name.startswith("NSE_") and d.name.endswith("_EQ")])

    print(f"Scanning {len(sym_dirs)} symbols for >{args.multiplier}x volume spikes "
          f"(rolling {WINDOW_BARS}-bar prior max ≈ 30 calendar days)...\n")

    all_hits = []
    for sym_dir in sym_dirs:
        sym = sym_dir.name
        df  = load_symbol(sym_dir)
        if df is None or "volume" not in df.columns or len(df) < 100:
            continue
        hits = scan_symbol(sym, df, args.multiplier)
        if not hits.empty:
            all_hits.append(hits)
            print(f"  {sym:<30}  {len(hits):>3} spike(s)")

    if not all_hits:
        print("No spikes found.")
        return

    result = pd.concat(all_hits).sort_index()
    result.index.name = "datetime"

    out_csv = OUT_DIR / f"volume_spikes_{args.multiplier}x_avg.csv"
    result.to_csv(out_csv)

    print(f"\n{'='*60}")
    print(f"  Total spikes found : {len(result)}")
    print(f"  Unique symbols     : {result['symbol'].nunique()}")
    print(f"  Date range         : {result.index.min().date()} → {result.index.max().date()}")
    print(f"  Saved to           : {out_csv}")
    print(f"{'='*60}\n")

    # top 20 by ratio
    top = result.nlargest(20, "ratio")[["symbol", "volume_spike", "avg_vol_30d", "ratio", "close"]]
    top.index = top.index.strftime("%Y-%m-%d %H:%M")
    print("  TOP 20 SPIKES BY RATIO:")
    print(f"  {'DateTime':<18} {'Symbol':<16} {'Spike Vol':>12} {'30d Avg':>12} {'Ratio':>8} {'Close':>10}")
    print("  " + "-"*80)
    for dt, row in top.iterrows():
        print(f"  {dt:<18} {row['symbol']:<16} {int(row['volume_spike']):>12,} "
              f"{int(row['avg_vol_30d']):>12,} {row['ratio']:>7.0f}x {row['close']:>10.2f}")
    print()


if __name__ == "__main__":
    main()
