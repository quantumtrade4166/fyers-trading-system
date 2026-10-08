import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
BTCUSDT 1-minute history from Binance's public archive
=========================================================
Source: https://data.binance.vision  (free, no key, one zip per month)
Range : 2017-08 (Binance launch) -> the last complete day

Output: data/CRYPTO_BTCUSDT/{year}/ohlcv_1m_{MM}.parquet
        columns datetime (UTC, naive), open, high, low, close, volume (BTC)

Why Binance and not the exchange we'd trade on: Delta Exchange India only has
history from late 2023. Binance covers 2018 (-84%) and 2022 (-77%) — without
those two crashes a BTC backtest has never seen a real bear market. Delta's
perp is downloaded separately (download_delta_btc.py) to check that signals on
Binance prices transfer to the venue we'd actually fill on.

Re-runnable: a month is skipped once its parquet exists. The current month is
rebuilt from daily zips on every run (the monthly zip only appears after the
month ends).

Run: .venv\\Scripts\\python.exe crypto\\download_binance_btc.py
"""

import io
import time
import zipfile
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1] / "data" / "CRYPTO_BTCUSDT"
BASE = "https://data.binance.vision/data/spot"
SYMBOL = "BTCUSDT"
FIRST_MONTH = date(2017, 8, 1)

COLS = ["open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]


def _fetch_zip(url: str, retries: int = 4) -> bytes | None:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fyers-pipeline/btc-history"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except Exception as e:
            err = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url}: {err}")


def _parse(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        with z.open(z.namelist()[0]) as f:
            df = pd.read_csv(f, header=None, names=COLS)
    # Some files carry a header row; drop anything non-numeric.
    df = df[pd.to_numeric(df["open_time"], errors="coerce").notna()]
    ts = df["open_time"].astype("int64")
    # Binance switched spot files from milliseconds to MICROseconds in 2025.
    unit = "us" if ts.iloc[0] > 10**14 else "ms"
    out = pd.DataFrame({
        "datetime": pd.to_datetime(ts, unit=unit),
        "open":   df["open"].astype(float),
        "high":   df["high"].astype(float),
        "low":    df["low"].astype(float),
        "close":  df["close"].astype(float),
        "volume": df["volume"].astype(float),
    })
    return out


def _month_path(m: date) -> Path:
    return ROOT / str(m.year) / f"ohlcv_1m_{m.month:02d}.parquet"


def _save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = df.drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
    df.to_parquet(path, index=False)


def main() -> None:
    today = date.today()
    this_month = today.replace(day=1)
    months = []
    m = FIRST_MONTH
    while m < this_month:
        months.append(m)
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)

    print(f"BTCUSDT 1m -> {ROOT}")
    got = skipped = 0
    for m in months:
        p = _month_path(m)
        if p.exists():
            skipped += 1
            continue
        url = f"{BASE}/monthly/klines/{SYMBOL}/1m/{SYMBOL}-1m-{m:%Y-%m}.zip"
        raw = _fetch_zip(url)
        if raw is None:
            print(f"  {m:%Y-%m}  missing on archive")
            continue
        df = _parse(raw)
        _save(df, p)
        got += 1
        print(f"  {m:%Y-%m}  {len(df):>6,} bars  close {df['close'].iloc[-1]:>10,.0f}")

    # Current month from daily files (up to yesterday).
    frames = []
    d = this_month
    while d < today:
        url = f"{BASE}/daily/klines/{SYMBOL}/1m/{SYMBOL}-1m-{d:%Y-%m-%d}.zip"
        raw = _fetch_zip(url)
        if raw is not None:
            frames.append(_parse(raw))
        d += timedelta(days=1)
    if frames:
        df = pd.concat(frames)
        _save(df, _month_path(this_month))
        print(f"  {this_month:%Y-%m}  {len(df):>6,} bars (partial, {len(frames)} days)")

    print(f"\nDone: {got} months downloaded, {skipped} already present.")


if __name__ == "__main__":
    main()
