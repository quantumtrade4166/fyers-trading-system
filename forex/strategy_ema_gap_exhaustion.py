import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
EMA Gap Exhaustion Reversal — XAUUSD, long + short
======================================================
RULES (short side):
  1. Z-score of the EMA9/EMA21 gap (same definition as the mean-reversion
     strategy: gap_pct = (ema9-ema21)/close*100, rolling 100-bar mean/std)
     is above +z_threshold
  2. 2+ consecutive GREEN candles (close > open) with close > EMA9
  3. Then a RED candle (close < open) appears -> this is the "signal candle"
     (only qualifies if z > +z_threshold on THIS signal candle)
  4. Entry   : SHORT when a later bar's LOW breaks the signal candle's LOW
  5. Stop    : signal candle HIGH
  6. Exit    : z reverts to 0  OR  stop loss hit

LONG side is the exact mirror: z < -z_threshold, 2+ red candles below EMA9,
then a green signal candle, entry above its high, SL below its low.

Run: python forex/run_backtest_ema_gap_exhaustion.py
"""

import logging
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "fast_ema":      9,
    "slow_ema":      21,
    "lookback":      100,
    "z_threshold":   2.5,
    "min_streak":    2,
    "capital":       20_000,
    "risk_per_trade_pct": 0.01,
    "max_position_pct": 0.50,
}


@dataclass
class Trade:
    direction:   str            # 'long' | 'short'
    signal_time: pd.Timestamp
    entry_time:  pd.Timestamp
    entry_price: float
    stop_loss:   float
    signal_z:    float
    streak_bars: int
    units:       float
    exit_time:   Optional[pd.Timestamp] = None
    exit_price:  Optional[float]        = None
    exit_reason: Optional[str]          = None   # stop | reverted | eod
    pnl:         float                  = 0.0

    @property
    def sl_distance(self) -> float:
        return abs(self.stop_loss - self.entry_price)

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sl_distance"] = self.sl_distance
        d["is_winner"]   = self.is_winner
        return d


@dataclass
class _Signal:
    direction:   str
    entry_price: float          # short -> signal low | long -> signal high
    stop_loss:   float          # short -> signal high | long -> signal low
    signal_time: pd.Timestamp
    signal_z:    float
    streak_bars: int


@dataclass
class _Position:
    direction:   str
    signal_time: pd.Timestamp
    entry_time:  pd.Timestamp
    entry_price: float
    stop_loss:   float
    signal_z:    float
    streak_bars: int
    units:       float


def _calc_units(risk_amount: float, sl_distance: float, entry_price: float, max_capital: float) -> float:
    if sl_distance <= 0:
        return 0.0
    risk_units    = risk_amount / sl_distance
    capital_units = max_capital / entry_price
    return min(risk_units, capital_units)


def _pnl(pos: _Position, exit_price: float) -> float:
    if pos.direction == "long":
        return (exit_price - pos.entry_price) * pos.units
    return (pos.entry_price - exit_price) * pos.units


def _close(pos: _Position, exit_time, exit_price, reason, pnl) -> Trade:
    return Trade(
        direction=pos.direction, signal_time=pos.signal_time, entry_time=pos.entry_time,
        entry_price=pos.entry_price, stop_loss=pos.stop_loss, signal_z=pos.signal_z,
        streak_bars=pos.streak_bars, units=pos.units,
        exit_time=exit_time, exit_price=exit_price, exit_reason=reason, pnl=round(pnl, 2),
    )


def run_backtest(df: pd.DataFrame, config: dict = None) -> list[Trade]:
    from backtesting.indicators import add_ema

    cfg = {**DEFAULT_CONFIG, **(config or {})}

    df = add_ema(df.copy(), period=cfg["fast_ema"])
    df = add_ema(df, period=cfg["slow_ema"])
    fast_col = f"ema_{cfg['fast_ema']}"
    slow_col = f"ema_{cfg['slow_ema']}"

    gap_pct = (df[fast_col] - df[slow_col]) / df["close"] * 100
    roll_mean = gap_pct.rolling(cfg["lookback"]).mean()
    roll_std  = gap_pct.rolling(cfg["lookback"]).std()
    z = (gap_pct - roll_mean) / roll_std

    revert_signal = (z.shift(1) > 0) & (z <= 0) | (z.shift(1) < 0) & (z >= 0)

    risk_amount = cfg["capital"] * cfg["risk_per_trade_pct"]
    max_cap     = cfg["capital"] * cfg["max_position_pct"]
    min_streak  = cfg["min_streak"]
    z_thr       = cfg["z_threshold"]

    trades:   list[Trade]         = []
    signal:   Optional[_Signal]   = None
    position: Optional[_Position] = None

    green_streak = 0   # consecutive green candles closing above EMA9
    red_streak   = 0   # consecutive red candles closing below EMA9

    bar_list = list(df.iterrows())

    for i, (ts, bar) in enumerate(bar_list):
        ema9_val = bar[fast_col]
        if pd.isna(z.iloc[i]) or pd.isna(ema9_val):
            continue

        close, high, low, open_ = bar["close"], bar["high"], bar["low"], bar["open"]
        is_green = close > open_
        is_red   = close < open_
        above_ema9 = close > ema9_val
        below_ema9 = close < ema9_val

        # ── Manage open position ────────────────────────────────────────────
        if position is not None:
            stopped = (
                (position.direction == "short" and high >= position.stop_loss) or
                (position.direction == "long" and low <= position.stop_loss)
            )
            if stopped:
                pnl = _pnl(position, position.stop_loss)
                trades.append(_close(position, ts, position.stop_loss, "stop", pnl))
                position = None
            elif revert_signal.iloc[i]:
                pnl = _pnl(position, close)
                trades.append(_close(position, ts, close, "reverted", pnl))
                position = None

            green_streak = 1 if (is_green and above_ema9) else 0
            red_streak   = 1 if (is_red and below_ema9) else 0
            continue

        # ── Pending signal: watch for breakout trigger ──────────────────────
        if signal is not None:
            triggered = (
                (signal.direction == "short" and low <= signal.entry_price) or
                (signal.direction == "long" and high >= signal.entry_price)
            )
            if triggered:
                units = _calc_units(risk_amount, abs(signal.stop_loss - signal.entry_price),
                                     signal.entry_price, max_cap)
                if units > 0:
                    position = _Position(
                        direction=signal.direction, signal_time=signal.signal_time, entry_time=ts,
                        entry_price=signal.entry_price, stop_loss=signal.stop_loss,
                        signal_z=signal.signal_z, streak_bars=signal.streak_bars, units=units,
                    )
                signal = None
                green_streak = 1 if (is_green and above_ema9) else 0
                red_streak   = 1 if (is_red and below_ema9) else 0
                continue
            # not triggered yet -> fall through to update streaks / detect a fresh signal below

        # ── Detect a signal candle using the streak BEFORE this bar ─────────
        # (any red candle can break a green streak, regardless of its own
        #  EMA position, and vice versa for green candles breaking a red streak)
        if is_red and green_streak >= min_streak and z.iloc[i] > z_thr:
            signal = _Signal(
                direction="short", entry_price=low, stop_loss=high,
                signal_time=ts, signal_z=round(z.iloc[i], 3), streak_bars=green_streak,
            )
        elif is_green and red_streak >= min_streak and z.iloc[i] < -z_thr:
            signal = _Signal(
                direction="long", entry_price=high, stop_loss=low,
                signal_time=ts, signal_z=round(z.iloc[i], 3), streak_bars=red_streak,
            )

        # ── Update streaks using THIS bar's own classification ──────────────
        if is_green and above_ema9:
            green_streak += 1
            red_streak = 0
        elif is_red and below_ema9:
            red_streak += 1
            green_streak = 0
        else:
            green_streak = 0
            red_streak = 0

    if position is not None and bar_list:
        last_ts, last_bar = bar_list[-1]
        pnl = _pnl(position, last_bar["close"])
        trades.append(_close(position, last_ts, last_bar["close"], "eod", pnl))

    return trades
