import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Nasdaq 100 CFD Data Loader
===========================
Loads the 1-minute OHLC archive from Dukascopy (usatechidxusd)
and resamples to any working timeframe for backtesting.

Output path: H:\\My Drive\\Dukascopy_1m\\usatechidxusd\\{year}\\ohlcv_1m_{MM}.parquet

Usage:
    from forex.nasdaq_data_loader import load_nasdaq

    df_5min = load_nasdaq(start="2021-01-01", end="2026-08-31", timeframe="5min")
"""

import logging
import pandas as pd
from pathlib import Path
from datetime import date

logger = logging.getLogger(__name__)

RAW_DIR = Path("H:/My Drive/Dukascopy_1m/usatechidxusd")


def _month_files(start: date, end: date) -> list[Path]:
    files = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        f = RAW_DIR / f"{y:04d}" / f"ohlcv_1m_{m:02d}.parquet"
        if f.exists():
            files.append(f)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return files


def resample_ohlc(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample 1m OHLC to a higher timeframe."""
    resampled = df.resample(timeframe, closed="left", label="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    )
    return resampled.dropna(subset=["open", "close"])


def load_nasdaq(start: str, end: str, timeframe: str = "5min") -> pd.DataFrame:
    """
    Load Nasdaq 100 (usatechidxusd) data for [start, end] (inclusive)
    and resample to `timeframe`.

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
        raise FileNotFoundError(
            f"No Nasdaq data files found for {start} -> {end} in {RAW_DIR}\n"
            f"If Google Drive is not mounted, reconnect it first."
        )

    logger.info(f"Loading {len(files)} months of 1m data ({start} -> {end})")
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
    logger.info(f"Resampled {total_rows:,} 1m bars -> {len(resampled):,} {timeframe} bars")
    return resampled
