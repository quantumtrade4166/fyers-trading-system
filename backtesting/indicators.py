# ============================================================
# backtesting/indicators.py
#
# Technical indicators for Nifty F&O intraday backtesting.
# All indicators work on a datetime-indexed OHLCV DataFrame
# as returned by DataLoader.load().
#
# API convention — every indicator function:
#   • Accepts a DataFrame with columns: open, high, low, close, volume
#   • Adds one or more named columns to the DataFrame
#   • Returns the same DataFrame (modified in-place)
#   • Names columns deterministically: e.g. ema_20, rsi_14, bb_upper_20
#
# Usage:
#   from backtesting.indicators import add_ema, add_rsi, add_vwap
#   from backtesting.indicators import add_supertrend, add_bollinger
#   from backtesting.indicators import apply_indicators
#
#   df = loader.load("NSE:RELIANCE-EQ")
#   df = add_ema(df, period=20)
#   df = add_ema(df, period=50)
#   df = add_rsi(df, period=14)
#   df = add_vwap(df)
#   df = add_supertrend(df, period=10, multiplier=3.0)
#   df = add_bollinger(df, period=20, std_dev=2.0)
#
#   # Or all at once via config dict:
#   df = apply_indicators(df, {
#       "ema": [9, 21, 50],
#       "rsi": [14],
#       "vwap": True,
#       "supertrend": {"period": 10, "multiplier": 3.0},
#       "bollinger": {"period": 20, "std_dev": 2.0},
#   })
# ============================================================

import sys
import logging
from typing import Optional, Union

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger(__name__)


# ── Validation helper ─────────────────────────────────────────────────────────

def _validate_df(df: pd.DataFrame, required_cols: list[str]) -> None:
    """Raise ValueError if any required column is missing."""
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"DataFrame is missing required columns: {missing}\n"
            f"Available columns: {df.columns.tolist()}"
        )


# ── EMA — Exponential Moving Average ─────────────────────────────────────────

def add_ema(
    df: pd.DataFrame,
    period: int,
    column: str = "close",
    col_name: Optional[str] = None,
) -> pd.DataFrame:
    """
    Add an Exponential Moving Average column to the DataFrame.

    Uses standard EMA formula: alpha = 2 / (period + 1)
    First value is seeded with the SMA of the first `period` rows.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with DatetimeIndex.
    period : int
        Lookback window, e.g. 9, 20, 50, 200.
    column : str, default "close"
        Source column to compute EMA on.
    col_name : str, optional
        Output column name. Defaults to f"ema_{period}".

    Returns
    -------
    pd.DataFrame
        Same DataFrame with new column added in-place.

    Added column
    ------------
    ema_{period}  (or col_name if provided)
    """
    _validate_df(df, [column])
    name = col_name or f"ema_{period}"
    df[name] = df[column].ewm(span=period, adjust=False).mean()
    logger.debug(f"Added {name} (period={period}, source={column})")
    return df


# ── RSI — Relative Strength Index (Wilder's smoothing) ───────────────────────

def add_rsi(
    df: pd.DataFrame,
    period: int = 14,
    column: str = "close",
    col_name: Optional[str] = None,
) -> pd.DataFrame:
    """
    Add a Relative Strength Index column using Wilder's smoothing method.

    Wilder's smoothing is equivalent to EMA with alpha = 1 / period.
    Values range 0–100. First `period` values will be NaN (warmup).

    Parameters
    ----------
    df : pd.DataFrame
    period : int, default 14
    column : str, default "close"
    col_name : str, optional
        Defaults to f"rsi_{period}".

    Returns
    -------
    pd.DataFrame

    Added column
    ------------
    rsi_{period}
    """
    _validate_df(df, [column])
    name = col_name or f"rsi_{period}"

    delta = df[column].diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)

    # Wilder's smoothing = EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss
    df[name] = 100.0 - (100.0 / (1.0 + rs))

    # The first `period` rows are unreliable — set to NaN
    df.loc[df.index[:period], name] = np.nan

    logger.debug(f"Added {name} (period={period})")
    return df


# ── VWAP — Volume Weighted Average Price (daily reset) ───────────────────────

def add_vwap(
    df: pd.DataFrame,
    col_name: str = "vwap",
) -> pd.DataFrame:
    """
    Add an intraday VWAP column that resets at 09:15 each day.

    Formula: VWAP(t) = Σ(TP × Volume) / Σ(Volume)
    where Typical Price = (High + Low + Close) / 3
    and Σ is cumulative within the trading day.

    VWAP is calculated per calendar day so it resets correctly
    even across multi-year DataFrames.

    Parameters
    ----------
    df : pd.DataFrame
        Must have a DatetimeIndex and columns: high, low, close, volume.
    col_name : str, default "vwap"

    Returns
    -------
    pd.DataFrame

    Added column
    ------------
    vwap
    """
    _validate_df(df, ["high", "low", "close", "volume"])

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol = typical_price * df["volume"]

    # Group by calendar date and compute intraday cumulative sums
    date_key = df.index.date
    cum_tp_vol = tp_vol.groupby(date_key).cumsum()
    cum_vol    = df["volume"].groupby(date_key).cumsum()

    df[col_name] = cum_tp_vol / cum_vol

    logger.debug(f"Added {col_name} (daily reset)")
    return df


# ── Supertrend ────────────────────────────────────────────────────────────────

def add_supertrend(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """
    Add Supertrend indicator columns.

    Algorithm:
    1. True Range (TR) = max(H-L, |H-prev_C|, |L-prev_C|)
    2. ATR = Wilder's smoothed average of TR over `period` bars
    3. Basic Upper Band = (H+L)/2 + multiplier × ATR
    4. Basic Lower Band = (H+L)/2 - multiplier × ATR
    5. Final bands tighten progressively (never widen while in trend)
    6. Supertrend line follows the lower band in uptrend, upper band in downtrend

    Parameters
    ----------
    df : pd.DataFrame
        Must have columns: high, low, close.
    period : int, default 10
        ATR lookback period.
    multiplier : float, default 3.0
        Band width multiplier.

    Returns
    -------
    pd.DataFrame

    Added columns
    -------------
    supertrend_{period}_{mult}    — the supertrend price line
    supertrend_dir_{period}_{mult} — direction: 1 = uptrend, -1 = downtrend
    """
    _validate_df(df, ["high", "low", "close"])

    suffix = f"{period}_{multiplier}"
    st_col  = f"supertrend_{suffix}"
    dir_col = f"supertrend_dir_{suffix}"

    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    hl_avg = (high + low) / 2.0

    # ── ATR via Wilder's smoothing ────────────────────────────────────────────
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()

    # ── Basic bands ───────────────────────────────────────────────────────────
    basic_ub = hl_avg + multiplier * atr
    basic_lb = hl_avg - multiplier * atr

    # ── Final bands + Supertrend (requires iterative calculation) ────────────
    n = len(df)
    final_ub  = np.full(n, np.nan)
    final_lb  = np.full(n, np.nan)
    supertrend = np.full(n, np.nan)
    direction  = np.zeros(n, dtype=int)

    bub = basic_ub.values
    blb = basic_lb.values
    cl  = close.values

    for i in range(1, n):
        # Skip warmup (ATR needs `period` bars)
        if np.isnan(atr.iloc[i]):
            continue

        # ── Final upper band ──────────────────────────────────────────────
        if np.isnan(final_ub[i - 1]):
            final_ub[i] = bub[i]
        elif bub[i] < final_ub[i - 1] or cl[i - 1] > final_ub[i - 1]:
            final_ub[i] = bub[i]
        else:
            final_ub[i] = final_ub[i - 1]

        # ── Final lower band ──────────────────────────────────────────────
        if np.isnan(final_lb[i - 1]):
            final_lb[i] = blb[i]
        elif blb[i] > final_lb[i - 1] or cl[i - 1] < final_lb[i - 1]:
            final_lb[i] = blb[i]
        else:
            final_lb[i] = final_lb[i - 1]

        # ── Supertrend direction ──────────────────────────────────────────
        if np.isnan(supertrend[i - 1]):
            # First valid bar — start in whichever trend price suggests
            if cl[i] > final_ub[i]:
                supertrend[i] = final_lb[i]
                direction[i]  = 1
            else:
                supertrend[i] = final_ub[i]
                direction[i]  = -1
        elif supertrend[i - 1] == final_ub[i - 1]:
            # Was in downtrend
            if cl[i] > final_ub[i]:
                supertrend[i] = final_lb[i]
                direction[i]  = 1
            else:
                supertrend[i] = final_ub[i]
                direction[i]  = -1
        else:
            # Was in uptrend
            if cl[i] < final_lb[i]:
                supertrend[i] = final_ub[i]
                direction[i]  = -1
            else:
                supertrend[i] = final_lb[i]
                direction[i]  = 1

    df[st_col]  = supertrend
    df[dir_col] = direction

    # Mark warmup rows as NaN / 0
    warmup_mask = atr.isna()
    df.loc[warmup_mask, st_col]  = np.nan
    df.loc[warmup_mask, dir_col] = 0

    logger.debug(f"Added {st_col} and {dir_col} (period={period}, mult={multiplier})")
    return df


# ── Bollinger Bands ───────────────────────────────────────────────────────────

def add_bollinger(
    df: pd.DataFrame,
    period: int = 20,
    std_dev: float = 2.0,
    column: str = "close",
) -> pd.DataFrame:
    """
    Add Bollinger Band columns.

    Formula:
      Middle  = SMA(close, period)
      Upper   = Middle + std_dev × rolling_std
      Lower   = Middle − std_dev × rolling_std
      Width   = (Upper − Lower) / Middle × 100   [% of middle band]
      %B      = (close − Lower) / (Upper − Lower) [0=at lower, 1=at upper]

    Parameters
    ----------
    df : pd.DataFrame
    period : int, default 20
    std_dev : float, default 2.0
    column : str, default "close"

    Returns
    -------
    pd.DataFrame

    Added columns
    -------------
    bb_upper_{period}     — upper band
    bb_middle_{period}    — middle band (SMA)
    bb_lower_{period}     — lower band
    bb_width_{period}     — band width as % of middle band
    bb_pct_{period}       — %B: price position within bands (0–1, can exceed)
    """
    _validate_df(df, [column])

    p = period
    rolling = df[column].rolling(window=p)
    middle  = rolling.mean()
    std     = rolling.std(ddof=0)         # population std (matches TradingView)

    upper = middle + std_dev * std
    lower = middle - std_dev * std

    band_range = upper - lower

    df[f"bb_upper_{p}"]  = upper
    df[f"bb_middle_{p}"] = middle
    df[f"bb_lower_{p}"]  = lower
    df[f"bb_width_{p}"]  = (band_range / middle * 100).round(4)
    df[f"bb_pct_{p}"]    = ((df[column] - lower) / band_range).round(4)

    logger.debug(f"Added Bollinger Bands (period={p}, std_dev={std_dev})")
    return df


# ── Convenience batch function ────────────────────────────────────────────────

def apply_indicators(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """
    Apply multiple indicators in one call via a config dictionary.

    Parameters
    ----------
    df : pd.DataFrame
    config : dict
        Keys and values:

        "ema"        : list[int] — list of EMA periods
                       e.g. [9, 21, 50, 200]

        "rsi"        : list[int] — list of RSI periods
                       e.g. [14]

        "vwap"       : bool — True to add VWAP
                       e.g. True

        "supertrend" : dict | list[dict] — one or many configs
                       e.g. {"period": 10, "multiplier": 3.0}
                       or   [{"period": 7, "multiplier": 3.0},
                              {"period": 10, "multiplier": 3.0}]

        "bollinger"  : dict — {"period": 20, "std_dev": 2.0}
                       or list[dict] for multiple

    Returns
    -------
    pd.DataFrame with all requested indicator columns added.

    Example
    -------
    >>> df = apply_indicators(df, {
    ...     "ema": [9, 21, 50],
    ...     "rsi": [14],
    ...     "vwap": True,
    ...     "supertrend": {"period": 10, "multiplier": 3.0},
    ...     "bollinger": {"period": 20, "std_dev": 2.0},
    ... })
    """
    # EMA
    for period in config.get("ema", []):
        df = add_ema(df, period=period)

    # RSI
    for period in config.get("rsi", []):
        df = add_rsi(df, period=period)

    # VWAP
    if config.get("vwap"):
        df = add_vwap(df)

    # Supertrend (single dict or list of dicts)
    st_config = config.get("supertrend")
    if st_config:
        if isinstance(st_config, dict):
            st_config = [st_config]
        for cfg in st_config:
            df = add_supertrend(df, **cfg)

    # Bollinger Bands (single dict or list of dicts)
    bb_config = config.get("bollinger")
    if bb_config:
        if isinstance(bb_config, dict):
            bb_config = [bb_config]
        for cfg in bb_config:
            df = add_bollinger(df, **cfg)

    return df
