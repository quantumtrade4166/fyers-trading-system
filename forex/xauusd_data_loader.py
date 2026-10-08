import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
XAUUSD Data Loader
====================
Loads the 1-second OHLC parquet archive (H:\\My Drive\\XAUUSD_1s_Data\\)
and resamples it to any working timeframe for backtesting.

Usage:
    from forex.xauusd_data_loader import load_xauusd

    df_5min = load_xauusd(start="2023-08-19", end="2026-08-19", timeframe="5min")
"""

import logging
import pandas as pd
from pathlib import Path
from datetime import date

logger = logging.getLogger(__name__)

RAW_DIR = Path("H:/My Drive/XAUUSD_1s_Data")


def _month_files(start: date, end: date) -> list[Path]:
    files = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        f = RAW_DIR / f"{y:04d}" / f"ohlcv_1s_{m:02d}.parquet"
        if f.exists():
            files.append(f)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return files


def resample_ohlc(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample 1s OHLC (no volume) to a higher timeframe."""
    resampled = df.resample(timeframe, closed="left", label="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    )
    return resampled.dropna(subset=["open", "close"])


def load_xauusd(start: str, end: str, timeframe: str = "5min") -> pd.DataFrame:
    """
    Load XAUUSD data for [start, end] (inclusive) and resample to `timeframe`.

    Parameters
    ----------
    start, end : "YYYY-MM-DD" date strings
    timeframe  : pandas offset alias, e.g. "5min", "1min", "15min"

    Returns
    -------
    Datetime-indexed DataFrame with columns: open, high, low, close
    """
    start_d = pd.Timestamp(start).date()
    end_d   = pd.Timestamp(end).date()

    files = _month_files(start_d, end_d)
    if not files:
        raise FileNotFoundError(f"No XAUUSD data files found for {start} -> {end} in {RAW_DIR}")

    logger.info(f"Loading {len(files)} months of 1s data ({start} -> {end})")
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end) + pd.Timedelta(days=1)

    resampled_parts = []
    total_rows = 0
    for f in files:
        month_df = pd.read_parquet(f)
        month_df = month_df.set_index("datetime").sort_index()
        month_df = month_df[(month_df.index >= start_ts) & (month_df.index <= end_ts)]
        total_rows += len(month_df)
        resampled_parts.append(resample_ohlc(month_df, timeframe))
        del month_df

    resampled = pd.concat(resampled_parts).sort_index()
    logger.info(f"Resampled {total_rows:,} 1s bars -> {len(resampled):,} {timeframe} bars")
    return resampled
