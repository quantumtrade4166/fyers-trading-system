import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Delta Exchange India BTCUSD perpetual — hourly history (cross-check set)
===========================================================================
The venue we would actually trade. History starts ~Dec 2023, so it is too
short to backtest on; it exists to confirm that a strategy tuned on Binance
BTCUSDT produces the same signals on Delta's perp prices.

Also pulls the perp's funding-rate history if the symbol is listed, so the
backtest's funding assumption can be checked against reality.

Output: data/CRYPTO_BTCUSDT/delta_btcusd_1h.parquet
Run   : .venv\\Scripts\\python.exe crypto\\download_delta_btc.py
"""

import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[1]))
from live_trading_options.delta_btc.core.api import candles

OUT = Path(__file__).parents[1] / "data" / "CRYPTO_BTCUSDT" / "delta_btcusd_1h.parquet"
START = int(pd.Timestamp("2023-06-01").timestamp())
STEP = 3600 * 1500          # stay under the 2000-candles-per-call cap


def fetch(symbol: str, resolution: str = "1h") -> pd.DataFrame:
    rows, t = [], START
    end_all = int(time.time())
    while t < end_all:
        t1 = min(t + STEP, end_all)
        rows += candles(symbol, resolution, t, t1)
        t = t1
        time.sleep(0.25)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["time"], unit="s")
    df = (df.drop(columns="time").drop_duplicates("datetime")
            .sort_values("datetime").reset_index(drop=True))
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df = fetch("BTCUSD")
    # Early listing hours are stale prints (volume 0, flat OHLC) — keep them in
    # the file but report where real trading begins.
    live = df[df["volume"] > 0]
    df.to_parquet(OUT, index=False)
    print(f"BTCUSD perp 1h: {len(df):,} bars  {df['datetime'].min()} -> {df['datetime'].max()}")
    print(f"  first bar with volume: {live['datetime'].min()}  ({len(live):,} traded bars)")

    try:
        fr = fetch("FUNDING:BTCUSD")
        if len(fr):
            fr.to_parquet(OUT.with_name("delta_btcusd_funding_1h.parquet"), index=False)
            print(f"  funding history: {len(fr):,} rows")
    except Exception as e:
        print(f"  funding history unavailable: {e}")


if __name__ == "__main__":
    main()
