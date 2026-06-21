# ============================================================
# backtesting/resample.py
#
# Resample 5-min OHLCV data to any higher timeframe.
# Works on datetime-indexed DataFrames as returned by DataLoader.
#
# Usage:
#   from backtesting.resample import resample_ohlcv
#   df_15 = resample_ohlcv(df_5min, "15min")
#   df_30 = resample_ohlcv(df_5min, "30min")
#   df_60 = resample_ohlcv(df_5min, "60min")
# ============================================================

import sys
import logging
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger(__name__)


def resample_ohlcv(df: pd.DataFrame, timeframe: str = "15min") -> pd.DataFrame:
    """
    Resample a 5-min OHLCV DataFrame to a higher timeframe.

    OHLCV aggregation rules:
      Open   = first bar's open in the window
      High   = max of all highs in the window
      Low    = min of all lows in the window
      Close  = last bar's close in the window
      Volume = sum of all volumes in the window

    The symbol column (if present) is preserved from the source.

    Parameters
    ----------
    df : pd.DataFrame
        Datetime-indexed 5-min OHLCV DataFrame from DataLoader.
        Must have columns: open, high, low, close, volume.
    timeframe : str, default "15min"
        Target timeframe. Examples: "15min", "30min", "60min", "1h".
        Any pandas offset alias works.

    Returns
    -------
    pd.DataFrame
        Resampled OHLCV DataFrame with same column structure.
        Incomplete bars (e.g. final bar with < 3 five-min candles) are
        included — they're real market data, just shorter windows.

    Notes
    -----
    - closed="left", label="left": bar labelled at its open time.
      The 9:15–9:30 window is labelled 9:15. ✓
    - 9:15 is exactly on a 15-min grid from midnight (555 min / 15 = 37),
      so pandas alignment is natural — no offset needed.
    - Rows with NaN open/close (truly empty windows) are dropped.
    """
    symbol = df["symbol"].iloc[0] if "symbol" in df.columns else None

    resampled = df.resample(timeframe, closed="left", label="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )

    # Drop windows with no data (gaps in the source data)
    resampled = resampled.dropna(subset=["open", "close"])

    if symbol is not None:
        resampled["symbol"] = symbol

    logger.debug(
        f"Resampled {len(df)} × 5-min bars → {len(resampled)} × {timeframe} bars"
    )
    return resampled
