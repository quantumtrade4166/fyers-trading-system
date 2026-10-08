import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
9/21 EMA Crossover — XAUUSD 5-min
====================================
RULES:
  1. Indicators : EMA9 and EMA21 on 5-min close
  2. Signal     : bar where EMA9 crosses EMA21
                    bullish cross (9 crosses above 21) -> long signal
                    bearish cross (9 crosses below 21) -> short signal
  3. Entry      : at the CLOSE of the crossover bar (same bar, no next-bar trigger)
  4. Stop loss  : long  -> low  of the crossover bar
                  short -> high of the crossover bar
  5. Exit       : stop loss hit  OR  opposite crossover (whichever comes first)
                  On an opposite crossover, the position closes AND immediately
                  reverses into the new signal at the same bar's close
                  (always-in-market system).
  6. Sizing     : risk_per_trade / sl_distance, capped by max capital exposure

Run: python forex/run_backtest_ema_crossover.py
"""

import logging
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "fast_ema":        9,
    "slow_ema":        21,
    "capital":         100_000,   # USD
    "risk_per_trade":  1_000,     # USD risk per trade (1% of capital)
    "max_position_pct": 0.50,     # cap notional exposure per trade
    "trend_ema":       None,      # e.g. 100 -> only long above it / short below it
    "min_sl_distance": None,      # e.g. 1.5 -> skip signals with a tinier SL gap (whipsaw filter)
}


@dataclass
class Trade:
    direction:   str            # 'long' | 'short'
    signal_time: pd.Timestamp
    entry_price: float
    stop_loss:   float
    units:       float
    exit_time:   Optional[pd.Timestamp] = None
    exit_price:  Optional[float]        = None
    exit_reason: Optional[str]          = None   # stop | reverse | eod
    pnl:         float                  = 0.0

    @property
    def sl_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss)

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
    direction:   str
    signal_time: pd.Timestamp
    entry_price: float
    stop_loss:   float
    units:       float


def _calc_units(risk_per_trade: float, sl_distance: float, entry_price: float, max_capital: float) -> float:
    if sl_distance <= 0:
        return 0.0
    risk_units    = risk_per_trade / sl_distance
    capital_units = max_capital / entry_price
    return min(risk_units, capital_units)


def _pnl(pos: _Position, exit_price: float) -> float:
    if pos.direction == "long":
        return (exit_price - pos.entry_price) * pos.units
    return (pos.entry_price - exit_price) * pos.units


def _close(pos: _Position, exit_time, exit_price, reason, pnl) -> Trade:
    return Trade(
        direction    = pos.direction,
        signal_time  = pos.signal_time,
        entry_price  = pos.entry_price,
        stop_loss    = pos.stop_loss,
        units        = pos.units,
        exit_time    = exit_time,
        exit_price   = exit_price,
        exit_reason  = reason,
        pnl           = round(pnl, 2),
    )


def _open_position(direction: str, ts, bar, cfg: dict) -> Optional[_Position]:
    entry_price = bar["close"]
    stop_loss   = bar["low"] if direction == "long" else bar["high"]
    sl_distance = abs(entry_price - stop_loss)

    if cfg.get("min_sl_distance") and sl_distance < cfg["min_sl_distance"]:
        return None

    if cfg.get("trend_ema"):
        trend_val = bar.get(f"ema_{cfg['trend_ema']}")
        if pd.isna(trend_val):
            return None
        if direction == "long" and entry_price <= trend_val:
            return None
        if direction == "short" and entry_price >= trend_val:
            return None

    max_cap = cfg["capital"] * cfg["max_position_pct"]
    units   = _calc_units(cfg["risk_per_trade"], sl_distance, entry_price, max_cap)
    if units <= 0:
        return None

    return _Position(
        direction   = direction,
        signal_time = ts,
        entry_price = entry_price,
        stop_loss   = stop_loss,
        units       = units,
    )


def run_backtest(df: pd.DataFrame, config: dict = None) -> list[Trade]:
    """
    Run the 9/21 EMA crossover strategy on a 5-min OHLC DataFrame.

    Parameters
    ----------
    df : datetime-indexed DataFrame with columns open, high, low, close
    config : overrides for DEFAULT_CONFIG

    Returns
    -------
    list[Trade] — all completed trades (entered AND exited)
    """
    from backtesting.indicators import add_ema

    cfg = {**DEFAULT_CONFIG, **(config or {})}

    df = add_ema(df.copy(), period=cfg["fast_ema"])
    df = add_ema(df, period=cfg["slow_ema"])
    fast_col = f"ema_{cfg['fast_ema']}"
    slow_col = f"ema_{cfg['slow_ema']}"

    if cfg.get("trend_ema"):
        df = add_ema(df, period=cfg["trend_ema"])

    diff = df[fast_col] - df[slow_col]
    prev_diff = diff.shift(1)

    bullish_cross = (prev_diff <= 0) & (diff > 0)
    bearish_cross = (prev_diff >= 0) & (diff < 0)

    trades: list[Trade] = []
    position: Optional[_Position] = None

    bar_list = list(df.iterrows())

    for i, (ts, bar) in enumerate(bar_list):
        if pd.isna(diff.iloc[i]) or pd.isna(prev_diff.iloc[i]):
            continue   # EMA not warmed up yet

        is_bull = bullish_cross.iloc[i]
        is_bear = bearish_cross.iloc[i]

        # ── Manage open position ────────────────────────────────────────────
        if position is not None:
            stopped = (
                (position.direction == "long" and bar["low"] <= position.stop_loss) or
                (position.direction == "short" and bar["high"] >= position.stop_loss)
            )
            if stopped:
                pnl = _pnl(position, position.stop_loss)
                trades.append(_close(position, ts, position.stop_loss, "stop", pnl))
                position = None
                continue

            opposite_signal = (
                (position.direction == "long" and is_bear) or
                (position.direction == "short" and is_bull)
            )
            if opposite_signal:
                exit_price = bar["close"]
                pnl = _pnl(position, exit_price)
                trades.append(_close(position, ts, exit_price, "reverse", pnl))
                new_direction = "short" if position.direction == "long" else "long"
                position = _open_position(new_direction, ts, bar, cfg)
            continue

        # ── Flat: look for a new signal ─────────────────────────────────────
        if is_bull:
            position = _open_position("long", ts, bar, cfg)
        elif is_bear:
            position = _open_position("short", ts, bar, cfg)

    # ── Close any position still open at end of data ───────────────────────
    if position is not None and bar_list:
        last_ts, last_bar = bar_list[-1]
        pnl = _pnl(position, last_bar["close"])
        trades.append(_close(position, last_ts, last_bar["close"], "eod", pnl))

    return trades
