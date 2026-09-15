"""
5-minute Nifty bars + Supertrend(7,3) + daily standard floor pivots.

Both are built from the `spot` column embedded in the options data, so no
external Nifty feed is needed.

Bars are anchored to the 09:15 open and labelled by their CLOSE time, so a
signal read off a bar is only actionable at the timestamp it carries -- no
lookahead.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backtesting.indicators import add_supertrend

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

BARS_PER_DAY = 75  # 375 min / 5


def build_5min_bars(spot_1min: pd.Series) -> pd.DataFrame:
    """1-min spot -> 5-min OHLC, bars anchored at 09:15 and labelled by close time."""
    df = spot_1min.to_frame("price")
    day_start = pd.Series(df.index.normalize() + pd.Timedelta(hours=9, minutes=15), index=df.index)
    minutes_since_open = (df.index.to_series() - day_start).dt.total_seconds() / 60.0
    bin_idx = (minutes_since_open // 5).astype(int).clip(upper=BARS_PER_DAY - 1)
    df["bin_label"] = day_start + (bin_idx + 1) * pd.Timedelta(minutes=5)

    bars = df.groupby("bin_label")["price"].agg(["first", "max", "min", "last"])
    bars.columns = ["open", "high", "low", "close"]
    return bars


def add_supertrend_7_3(bars: pd.DataFrame, period: int = 7, multiplier: float = 3.0) -> pd.DataFrame:
    """Supertrend computed on the continuous 5-min series (not reset daily),
    matching how charting platforms draw it."""
    return add_supertrend(bars, period=period, multiplier=multiplier)


def daily_ohlc_from_spot(spot_1min: pd.Series) -> pd.DataFrame:
    """Per-session High/Low/Close of Nifty spot (09:15-15:30)."""
    daily = spot_1min.groupby(spot_1min.index.normalize()).agg(["first", "max", "min", "last"])
    daily.columns = ["open", "high", "low", "close"]
    return daily


def build_daily_pivots(spot_1min: pd.Series) -> pd.DataFrame:
    """Standard (floor) pivots for each trading day, computed from the PREVIOUS
    session's H/L/C:

        PP = (H + L + C) / 3
        R1 = 2*PP - L
        S1 = 2*PP - H

    Rows whose previous session is unavailable (first day of the dataset, and
    the first day after each December data gap) are dropped -- those days
    cannot be traded.
    """
    daily = daily_ohlc_from_spot(spot_1min)
    prev = daily.shift(1)

    pp = (prev["high"] + prev["low"] + prev["close"]) / 3.0
    piv = pd.DataFrame({
        "pp": pp,
        "r1": 2 * pp - prev["low"],
        "s1": 2 * pp - prev["high"],
        "prev_high": prev["high"], "prev_low": prev["low"], "prev_close": prev["close"],
    })
    piv = piv.dropna()

    # Drop days whose "previous session" is actually across a multi-week data
    # gap (the Dec-Jan blackout) -- those pivots would be meaningless.
    gap_days = daily.index.to_series().diff().dt.days
    valid = gap_days[gap_days <= 5].index
    piv = piv.loc[piv.index.intersection(valid)]

    piv.index = piv.index.strftime("%Y-%m-%d")
    return piv
