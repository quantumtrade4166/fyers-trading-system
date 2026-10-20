import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Delta Exchange India perp funding history for the coins xs_taker can trade.

Source : Delta history candles, symbol FUNDING:{PERP} (1h). The value is the
         funding rate in % per 8 hours (0.01 = 0.01%/8h = 10.95% a year, matching
         the product's `annualized_funding`). Accrued hourly as value/8.
Coins  : crypto/universe/delta_perps_liquidity.csv, 24h turnover >= --min-turnover
Output : data/CRYPTO_PERPS_DELTA/{DELTA_SYMBOL}/funding_1h.parquet
         datetime (UTC, hour start), rate_8h_pct

Run: .venv\\Scripts\\python.exe crypto\\download_delta_funding.py
"""

import argparse
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from live_trading_options.delta_btc.core.api import candles  # noqa: E402

OUT = ROOT / "data" / "CRYPTO_PERPS_DELTA"
STEP = 3600 * 1500


def fetch(symbol: str, start: int) -> pd.DataFrame:
    rows, t, end_all = [], start, int(time.time())
    while t < end_all:
        t1 = min(t + STEP, end_all)
        rows += candles(symbol, "1h", t, t1)
        t = t1
        time.sleep(0.25)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["time"], unit="s")
    df = df.drop_duplicates("datetime").sort_values("datetime")
    return df.rename(columns={"close": "rate_8h_pct"})[["datetime", "rate_8h_pct"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-turnover", type=float, default=50e3)
    ap.add_argument("--start", default="2024-01-01")
    args = ap.parse_args()
    liq = pd.read_csv(ROOT / "crypto" / "universe" / "delta_perps_liquidity.csv")
    syms = liq.loc[(liq["turnover_24h_usd"] >= args.min_turnover) & liq["binance"].notna(), "delta"]
    start = int(pd.Timestamp(args.start).timestamp())
    for i, s in enumerate(syms, 1):
        try:
            df = fetch(f"FUNDING:{s}", start)
        except Exception as e:                       # report, never silently skip
            print(f"[{i:>3}/{len(syms)}] {s:<12} FAILED: {e}", flush=True)
            continue
        if df.empty:
            print(f"[{i:>3}/{len(syms)}] {s:<12} no funding history", flush=True)
            continue
        d = OUT / s
        d.mkdir(parents=True, exist_ok=True)
        df.to_parquet(d / "funding_1h.parquet", index=False)
        apr = df["rate_8h_pct"].mean() / 100 * 3 * 365
        print(f"[{i:>3}/{len(syms)}] {s:<12} {len(df):>6} h  {df['datetime'].min():%Y-%m-%d} -> "
              f"{df['datetime'].max():%Y-%m-%d}  mean APR {apr:6.1%}", flush=True)


if __name__ == "__main__":
    main()
