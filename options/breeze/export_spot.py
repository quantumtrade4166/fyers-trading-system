"""Extract the daily NIFTY spot series into a small CSV.

The downloader needs one number per trading day — the spot — to centre its
ATM+/-N strike window. It normally reads that from
data/NSE_NIFTY_OPTIONS/{year}/ohlcv_1min.parquet, which is 333MB and gitignored,
so it exists only on the machine that built it.

The long download runs on the VPS, which has neither. Shipping 333MB to recover
a few hundred floats is silly; this writes them to a ~30KB CSV instead.

    python -m options.breeze.export_spot            # write the CSV
    python -m options.breeze.export_spot --push     # write it and scp to VPS
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import subprocess

import pandas as pd

from options.breeze.config import DATA_DIR, PROJECT_ROOT

SOURCE_DIR = PROJECT_ROOT / "data" / "NSE_NIFTY_OPTIONS"
OUT_PATH = DATA_DIR / "nifty_spot_daily.csv"

VPS = "Administrator@144.79.166.103"
VPS_DIR = "C:/Users/Administrator/Desktop/fyers_data_pipeline_git/data/BREEZE_OPTIONS"


def export() -> pd.DataFrame:
    rows = []
    for year_dir in sorted(SOURCE_DIR.glob("[0-9][0-9][0-9][0-9]")):
        parquet = year_dir / "ohlcv_1min.parquet"
        if not parquet.exists():
            continue
        df = pd.read_parquet(parquet, columns=["date", "spot"])
        daily = df.groupby("date")["spot"].first().reset_index()
        rows.append(daily)
        print(f"  {year_dir.name}: {len(daily):>4} trading days")

    if not rows:
        raise SystemExit(f"No parquets under {SOURCE_DIR} — nothing to export.")

    out = pd.concat(rows, ignore_index=True)
    out["date"] = out["date"].astype(str)
    out = out.drop_duplicates("date").sort_values("date")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_PATH, index=False)

    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"\n✅ {len(out):,} days  {out['date'].min()} → {out['date'].max()}")
    print(f"   {OUT_PATH}  ({size_kb:.0f} KB)")
    return out


def push() -> None:
    print("\nCopying to VPS...")
    r = subprocess.run(["scp", str(OUT_PATH), f"{VPS}:{VPS_DIR}/nifty_spot_daily.csv"],
                       capture_output=True, text=True, timeout=120)
    print("✅ copied" if r.returncode == 0 else f"❌ scp failed: {r.stderr.strip()}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Export daily NIFTY spot to CSV")
    ap.add_argument("--push", action="store_true", help="also scp it to the VPS")
    args = ap.parse_args()

    export()
    if args.push:
        push()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
