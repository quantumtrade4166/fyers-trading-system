"""Candle building and Supertrend shared by the backtest and the live paper engine.

5-min candles are labelled at their open (09:15 = 09:15-09:19:59 one-minute bars),
built per session from 1-minute bars before 15:30 (the lone 15:30 bar is dropped).

Supertrend = backtesting.indicators.add_supertrend, which matches TradingView's
ta.supertrend (hl2 bands, Wilder/RMA ATR, close vs the current final band). The
first `period - 1` candles of a series are blanked so no value exists before ATR
has `period` true ranges behind it.
"""
import numpy as np
import pandas as pd

from backtesting.indicators import add_supertrend
from backtesting.resample import resample_ohlcv


def to_5min(one_min: pd.DataFrame, minutes: int = 5) -> pd.DataFrame:
    """1-minute bars (datetime column) -> session 5-min candles, labelled at open."""
    if one_min.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = one_min.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.drop_duplicates("datetime").sort_values("datetime")
    df = df[df["datetime"].dt.strftime("%H:%M") < "15:30"]
    if "volume" not in df.columns:
        df["volume"] = 0
    out = []
    for _, g in df.groupby(df["datetime"].dt.date):
        out.append(resample_ohlcv(g.set_index("datetime")[["open", "high", "low", "close", "volume"]],
                                  f"{minutes}min"))
    return pd.concat(out) if out else pd.DataFrame()


def supertrend(c5: pd.DataFrame, period: int, mult: float) -> pd.DataFrame:
    c5 = c5.copy()
    add_supertrend(c5, period=period, multiplier=mult)
    c5 = c5.rename(columns={f"supertrend_{period}_{mult}": "st",
                            f"supertrend_dir_{period}_{mult}": "st_dir"})
    c5.iloc[: period - 1, c5.columns.get_loc("st")] = np.nan
    c5.iloc[: period - 1, c5.columns.get_loc("st_dir")] = 0
    return c5


# NSE equity closing auction (NIFTY index, from 2026-08-03): the index stops
# ticking during the 15:15 minute and prints the official close at ~15:29:50.
# TradingView's NIFTY 5-min chart has NO candles after 15:10 on these days, and
# its Supertrend(10,3) is computed without them. Verified 13-Oct against 1,440
# TradingView candles (11-Sep..12-Oct): dropping index minutes >= 15:15 gives
# 100% direction agreement and 32/32 identical flips (ST within 3 pts). Keeping
# them -- raw Breeze holds junk there -- matched only 18/32 flips.
CLOSING_AUCTION_FROM = "2026-08-03"


def clean_index_closing_auction(one_min: pd.DataFrame, start: str = CLOSING_AUCTION_FROM) -> pd.DataFrame:
    """Drop index minutes from 15:15 on closing-auction days (TradingView convention).
    For the INDEX only -- options trade normally until 15:30."""
    df = one_min.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    hm = df["datetime"].dt.strftime("%H:%M")
    day = df["datetime"].dt.strftime("%Y-%m-%d")
    return df[~((day >= start) & (hm >= "15:15"))].reset_index(drop=True)
