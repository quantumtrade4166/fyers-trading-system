import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
EMA Gap Mean Reversion — XAUUSD, short-only
==============================================
RULES:
  1. Indicators : EMA9, EMA21 on close
  2. Gap        : gap_pct = (ema9 - ema21) / close * 100
                  (percent-of-price, not raw dollars -> comparable across
                   years even as gold's price level changes)
  3. Z-score    : rolling mean/std of gap_pct over `lookback` bars
                  z = (gap_pct - rolling_mean) / rolling_std
  4. Entry      : SHORT at close of the bar where z crosses above
                  +z_threshold (gap has stretched further than usual ->
                  bet it reverts back toward the rolling mean)
  5. Stop loss  : high of the entry bar (protective stop if the "stretch"
                  keeps extending instead of reverting)
  6. Exit       : z crosses back down to <= 0 (full reversion to the
                  rolling mean)  OR  stop loss hit

Run: python forex/run_backtest_ema_gap_sweep.py
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
    "lookback":      100,     # bars for rolling mean/std of the gap
    "z_threshold":   2.0,     # entry trigger: z > +threshold
    "capital":       20_000,  # USD
    "risk_per_trade_pct": 0.01,
    "max_position_pct": 0.50,
}


@dataclass
class Trade:
    signal_time: pd.Timestamp
    entry_price: float
    stop_loss:   float
    entry_z:     float
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
class _Position:
    signal_time: pd.Timestamp
    entry_price: float
    stop_loss:   float
    entry_z:     float
    units:       float


def _calc_units(risk_amount: float, sl_distance: float, entry_price: float, max_capital: float) -> float:
    if sl_distance <= 0:
        return 0.0
    risk_units    = risk_amount / sl_distance
    capital_units = max_capital / entry_price
    return min(risk_units, capital_units)


def run_backtest(df: pd.DataFrame, config: dict = None) -> list[Trade]:
    """
    Run the EMA-gap mean-reversion short strategy.

    Parameters
    ----------
    df : datetime-indexed DataFrame with columns open, high, low, close
    config : overrides for DEFAULT_CONFIG

    Returns
    -------
    list[Trade] — all completed trades
    """
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

    prev_z = z.shift(1)
    entry_signal = (prev_z <= cfg["z_threshold"]) & (z > cfg["z_threshold"])
    revert_signal = (prev_z > 0) & (z <= 0)

    risk_amount = cfg["capital"] * cfg["risk_per_trade_pct"]
    max_cap     = cfg["capital"] * cfg["max_position_pct"]

    trades: list[Trade] = []
    position: Optional[_Position] = None

    bar_list = list(df.iterrows())

    for i, (ts, bar) in enumerate(bar_list):
        if pd.isna(z.iloc[i]):
            continue

        if position is not None:
            # Stop loss first
            if bar["high"] >= position.stop_loss:
                pnl = (position.entry_price - position.stop_loss) * position.units
                trades.append(Trade(
                    signal_time=position.signal_time, entry_price=position.entry_price,
                    stop_loss=position.stop_loss, entry_z=position.entry_z, units=position.units,
                    exit_time=ts, exit_price=position.stop_loss, exit_reason="stop",
                    pnl=round(pnl, 2),
                ))
                position = None
                continue

            # Reversion exit
            if revert_signal.iloc[i]:
                exit_price = bar["close"]
                pnl = (position.entry_price - exit_price) * position.units
                trades.append(Trade(
                    signal_time=position.signal_time, entry_price=position.entry_price,
                    stop_loss=position.stop_loss, entry_z=position.entry_z, units=position.units,
                    exit_time=ts, exit_price=exit_price, exit_reason="reverted",
                    pnl=round(pnl, 2),
                ))
                position = None
            continue

        # Flat: look for a new short signal
        if entry_signal.iloc[i]:
            entry_price = bar["close"]
            stop_loss   = bar["high"]
            sl_distance = abs(stop_loss - entry_price)
            units = _calc_units(risk_amount, sl_distance, entry_price, max_cap)
            if units > 0:
                position = _Position(
                    signal_time=ts, entry_price=entry_price, stop_loss=stop_loss,
                    entry_z=round(z.iloc[i], 3), units=units,
                )

    if position is not None and bar_list:
        last_ts, last_bar = bar_list[-1]
        pnl = (position.entry_price - last_bar["close"]) * position.units
        trades.append(Trade(
            signal_time=position.signal_time, entry_price=position.entry_price,
            stop_loss=position.stop_loss, entry_z=position.entry_z, units=position.units,
            exit_time=last_ts, exit_price=last_bar["close"], exit_reason="eod",
            pnl=round(pnl, 2),
        ))

    return trades
