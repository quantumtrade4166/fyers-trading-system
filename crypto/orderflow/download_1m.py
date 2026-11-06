import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Binance USDT-M perps: 1-minute klines with taker-buy volume (orderflow base data)
=================================================================================
Every 1-min bar carries taker_buy_volume = volume bought by aggressive buyers.
  sell volume = volume - taker_buy_volume
  delta       = buy - sell            CVD = running sum of delta
This is the exact per-minute delta Binance computes from every trade.

Output (git-ignored): data/CRYPTO_PERPS/{PERP}/klines_1m.parquet
    datetime (UTC, naive, bar open), open, high, low, close, volume, quote_volume,
    trades, taker_buy_volume, taker_buy_quote

Re-runnable: resumes from the last stored month.

Run: .venv\\Scripts\\python.exe -m crypto.orderflow.download_1m --only BTCUSDT ETHUSDT --start 2020-01
"""

import argparse
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pandas as pd

from crypto.download_binance_perps import (BASE, KCOLS, OUT, _fetch, _merge_save,
                                           _months, _read_zip_csv)

KEEP = ["datetime", "open", "high", "low", "close", "volume", "quote_volume",
        "trades", "taker_buy_volume", "taker_buy_quote"]


def _to_frame(parts: list[pd.DataFrame]) -> pd.DataFrame:
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    ot = pd.to_numeric(df["open_time"])
    df["datetime"] = pd.to_datetime(ot, unit="us" if ot.max() > 1e14 else "ms")
    return df[KEEP].astype({c: "float64" for c in KEEP[1:]})


def klines_1m(perp: str, start: date, today: date, workers: int) -> pd.DataFrame:
    q = urllib.parse.quote(perp)
    this_month = date(today.year, today.month, 1)
    urls = [f"{BASE}/monthly/klines/{q}/1m/{q}-1m-{m:%Y-%m}.zip"
            for m in _months(start, this_month)]
    d = max(this_month, start)
    while d < today:
        urls.append(f"{BASE}/daily/klines/{q}/1m/{q}-1m-{d:%Y-%m-%d}.zip")
        d += timedelta(days=1)
    with ThreadPoolExecutor(workers) as ex:
        blobs = list(ex.map(_fetch, urls))
    parts = [_read_zip_csv(b, KCOLS) for b in blobs if b]
    return _to_frame(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    ap.add_argument("--start", default="2020-01", help="YYYY-MM")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    today = date.today()
    y, m = map(int, args.start.split("-"))
    for perp in args.only:
        t0 = time.time()
        path = OUT / perp / "klines_1m.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        start = date(y, m, 1)
        if path.exists():
            last = pd.read_parquet(path, columns=["datetime"])["datetime"].max().date()
            start = max(start, date(last.year, last.month, 1))
        df = klines_1m(perp, start, today, args.workers)
        n = _merge_save(path, df)
        full = pd.read_parquet(path, columns=["datetime"])["datetime"]
        gaps = (full.diff() > pd.Timedelta(minutes=1)).sum()
        print(f"{perp}: {n:,} bars {full.min()} -> {full.max()}  "
              f"gaps>1m: {gaps}  ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
