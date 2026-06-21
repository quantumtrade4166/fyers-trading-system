# ============================================================
# backtesting/strategy_bb_reversion.py
#
# Bollinger Band Mean Reversion Strategy — 15-min timeframe
#
# RULES:
#   Long  setup : ≥ 2 consecutive 15-min bars close BELOW lower BB
#                 → next bar closes back INSIDE the band (signal candle)
#                 → buy when price breaches signal candle HIGH
#                 → stop loss = signal candle LOW
#
#   Short setup : ≥ 2 consecutive 15-min bars close ABOVE upper BB
#                 → next bar closes back INSIDE the band (signal candle)
#                 → sell when price breaches signal candle LOW
#                 → stop loss = signal candle HIGH
#
#   Exit        : Hard exit at 3:00 PM bar close (last bar before 3:14)
#   Sizing      : shares = floor(risk_per_trade / SL_distance)
#                 capped at max_position_pct of capital
#
# ============================================================

import sys
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger(__name__)

# ── Strategy config defaults ──────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "timeframe":              "15min",
    "bb_period":              20,
    "bb_std":                 2.0,
    "min_outside_bars":       2,       # minimum consecutive bars outside BB
    "capital":                1_000_000, # ₹10,00,000
    "risk_per_trade":         10_000,  # ₹10,000 (1% of capital)
    "max_position_pct":       0.20,    # max 20% of capital in one position
    "exit_time":              "15:00", # bar timestamp at which open positions close
    # ── V2 filters ───────────────────────────────────────────────────────────
    "direction":              "both",  # 'both' | 'long' | 'short'
    "max_signal_candle_pct":  None,    # e.g. 0.008 → skip if (H-L)/close > 0.8%
    # ── V3 filters ───────────────────────────────────────────────────────────
    "ema_trend_filter":       False,   # True → only trade in trend direction
    "ema_trend_period":       50,      # EMA period on 5-min timeframe
    # short allowed only when signal candle close < 50 EMA (5-min)
    # long  allowed only when signal candle close > 50 EMA (5-min)
}

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol:       str
    direction:    str        # 'long' or 'short'
    signal_time:  pd.Timestamp
    entry_time:   pd.Timestamp
    entry_price:  float
    stop_loss:    float
    shares:       int
    exit_time:    Optional[pd.Timestamp] = None
    exit_price:   Optional[float]        = None
    exit_reason:  Optional[str]          = None  # 'stop' | 'stop_same_bar' | 'time_exit' | 'eod'
    pnl:          float                  = 0.0
    outside_bars: int                    = 0    # how many bars were outside before signal

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
class _Signal:
    """Pending entry order waiting to be triggered next bar."""
    direction:    str
    entry_price:  float   # long: signal high  | short: signal low
    stop_loss:    float   # long: signal low   | short: signal high
    signal_time:  pd.Timestamp
    outside_bars: int


@dataclass
class _Position:
    """Open position being managed."""
    symbol:      str
    direction:   str
    signal_time: pd.Timestamp
    entry_time:  pd.Timestamp
    entry_price: float
    stop_loss:   float
    shares:      int
    outside_bars: int


# ── Helpers ───────────────────────────────────────────────────────────────────

def _calc_shares(
    risk_per_trade: float,
    entry_price:    float,
    stop_loss:      float,
    max_capital:    float,
) -> int:
    """
    Calculate position size in shares.

    shares = min(
        floor(risk_per_trade / SL_distance),   # risk-based
        floor(max_capital    / entry_price)     # capital-based cap
    )

    Returns 0 if SL distance is zero (degenerate bar — skip trade).
    """
    sl_dist = abs(entry_price - stop_loss)
    if sl_dist <= 0:
        return 0
    risk_shares    = int(risk_per_trade / sl_dist)
    capital_shares = int(max_capital    / entry_price)
    return min(risk_shares, capital_shares)


def _pnl(position: _Position, exit_price: float) -> float:
    if position.direction == "long":
        return (exit_price - position.entry_price) * position.shares
    else:
        return (position.entry_price - exit_price) * position.shares


def _close(position: _Position, exit_time, exit_price, reason, pnl) -> Trade:
    return Trade(
        symbol       = position.symbol,
        direction    = position.direction,
        signal_time  = position.signal_time,
        entry_time   = position.entry_time,
        entry_price  = position.entry_price,
        stop_loss    = position.stop_loss,
        shares       = position.shares,
        exit_time    = exit_time,
        exit_price   = exit_price,
        exit_reason  = reason,
        pnl          = round(pnl, 2),
        outside_bars = position.outside_bars,
    )


# ── Core strategy engine ──────────────────────────────────────────────────────

def run_symbol(
    symbol:   str,
    df_5min:  pd.DataFrame,
    config:   dict,
) -> list[Trade]:
    """
    Run BB Reversion strategy on one symbol.

    Parameters
    ----------
    symbol  : Fyers symbol string, e.g. "NSE:RELIANCE-EQ"
    df_5min : 5-min OHLCV DataFrame from DataLoader (datetime-indexed)
    config  : Strategy config dict (see DEFAULT_CONFIG above)

    Returns
    -------
    list[Trade]  — all completed trades (entered AND exited)
    """
    from backtesting.resample    import resample_ohlcv
    from backtesting.indicators  import add_bollinger, add_ema

    cfg = {**DEFAULT_CONFIG, **config}

    # ── 1. Resample 5-min → 15-min ───────────────────────────────────────────
    df = resample_ohlcv(df_5min, cfg["timeframe"])
    if df.empty:
        return []

    # ── 2. 1-hour EMA trend filter ────────────────────────────────────────────
    # Resample 5-min → 1-hour, compute EMA there, then forward-fill into
    # 15-min bars. Each 15-min bar gets the most recently completed 1-hour
    # EMA value — no lookahead across hours.
    ema_col = None
    if cfg.get("ema_trend_filter"):
        ema_period  = cfg["ema_trend_period"]          # e.g. 50
        ema_tf      = cfg.get("ema_trend_timeframe", "1h")
        ema_col     = f"ema_{ema_period}_{ema_tf}"

        df_1h  = resample_ohlcv(df_5min, ema_tf)      # 5-min → 1-hour
        df_1h  = add_ema(df_1h, period=ema_period)     # 50 EMA on 1-hour bars

        # Forward-fill 1-hour EMA into 15-min index
        # reindex fills each 15-min bar with the last closed 1-hour EMA
        df[ema_col] = (
            df_1h[f"ema_{ema_period}"]
            .reindex(df.index, method="ffill")
        )

    # ── 3. Add Bollinger Bands on 15-min ─────────────────────────────────────
    df = add_bollinger(df, period=cfg["bb_period"], std_dev=cfg["bb_std"])

    bb_up  = f"bb_upper_{cfg['bb_period']}"
    bb_dn  = f"bb_lower_{cfg['bb_period']}"
    exit_t = pd.Timestamp(cfg["exit_time"]).time()
    max_cap = cfg["capital"] * cfg["max_position_pct"]

    # ── 3. Bar-by-bar simulation ──────────────────────────────────────────────
    trades:   list[Trade]         = []
    signal:   Optional[_Signal]   = None
    position: Optional[_Position] = None

    # Consecutive-bars-outside tracker
    outside_count = 0
    outside_side  = None   # 'above' | 'below'
    current_date  = None

    bar_list = list(df.iterrows())

    for idx, (ts, bar) in enumerate(bar_list):

        # ── Day boundary reset ────────────────────────────────────────────────
        if ts.date() != current_date:
            current_date  = ts.date()
            outside_count = 0
            outside_side  = None
            signal        = None          # cancel stale signal from prior day
            # Note: open positions are NOT reset here — time_exit handles them

        # Skip until Bollinger Bands have warmed up
        if pd.isna(bar[bb_up]):
            continue

        close  = bar["close"]
        high   = bar["high"]
        low    = bar["low"]
        bb_upper = bar[bb_up]
        bb_lower = bar[bb_dn]

        # ════════════════════════════════════════════════════════════════
        # BRANCH A — Manage open position
        # ════════════════════════════════════════════════════════════════
        if position is not None:

            # ── Time exit ─────────────────────────────────────────────
            if ts.time() >= exit_t:
                pnl = _pnl(position, close)
                trades.append(_close(position, ts, close, "time_exit", pnl))
                position      = None
                outside_count = 0
                outside_side  = None
                signal        = None
                continue

            # ── Stop loss hit ─────────────────────────────────────────
            stopped = (
                (position.direction == "long"  and low  <= position.stop_loss) or
                (position.direction == "short" and high >= position.stop_loss)
            )
            if stopped:
                exit_px = position.stop_loss
                pnl = _pnl(position, exit_px)
                trades.append(_close(position, ts, exit_px, "stop", pnl))
                position      = None
                outside_count = 0
                outside_side  = None
                signal        = None
                continue

            # Still in position — nothing else to do this bar
            continue

        # ════════════════════════════════════════════════════════════════
        # BRANCH B — Pending signal: check for entry trigger
        # ════════════════════════════════════════════════════════════════
        if signal is not None:

            # Cancel signal at exit time (no new entries in final bar)
            if ts.time() >= exit_t:
                signal        = None
                outside_count = 0
                outside_side  = None
                continue

            triggered = (
                (signal.direction == "long"  and high >= signal.entry_price) or
                (signal.direction == "short" and low  <= signal.entry_price)
            )

            if triggered:
                shares = _calc_shares(
                    cfg["risk_per_trade"],
                    signal.entry_price,
                    signal.stop_loss,
                    max_cap,
                )

                if shares > 0:
                    # Check if stopped in the same entry bar (worst case)
                    same_bar_stop = (
                        (signal.direction == "long"  and low  <= signal.stop_loss) or
                        (signal.direction == "short" and high >= signal.stop_loss)
                    )

                    if same_bar_stop:
                        # Entered and stopped out in the same bar
                        exit_px = signal.stop_loss
                        pnl = (
                            (signal.stop_loss - signal.entry_price) * shares
                            if signal.direction == "long"
                            else (signal.entry_price - signal.stop_loss) * shares
                        )
                        trades.append(Trade(
                            symbol       = symbol,
                            direction    = signal.direction,
                            signal_time  = signal.signal_time,
                            entry_time   = ts,
                            entry_price  = signal.entry_price,
                            stop_loss    = signal.stop_loss,
                            shares       = shares,
                            exit_time    = ts,
                            exit_price   = exit_px,
                            exit_reason  = "stop_same_bar",
                            pnl          = round(pnl, 2),
                            outside_bars = signal.outside_bars,
                        ))
                    else:
                        # Position opened, will be managed from next bar
                        position = _Position(
                            symbol       = symbol,
                            direction    = signal.direction,
                            signal_time  = signal.signal_time,
                            entry_time   = ts,
                            entry_price  = signal.entry_price,
                            stop_loss    = signal.stop_loss,
                            shares       = shares,
                            outside_bars = signal.outside_bars,
                        )

                signal = None   # order consumed

            # Signal not triggered yet — keep it active, skip new-signal detection
            continue

        # ════════════════════════════════════════════════════════════════
        # BRANCH C — No position, no signal: detect new setup
        # ════════════════════════════════════════════════════════════════
        if close > bb_upper:
            # Bar closed ABOVE upper band
            if outside_side == "above":
                outside_count += 1
            else:
                outside_side  = "above"
                outside_count = 1

        elif close < bb_lower:
            # Bar closed BELOW lower band
            if outside_side == "below":
                outside_count += 1
            else:
                outside_side  = "below"
                outside_count = 1

        else:
            # Bar closed INSIDE the band
            if outside_count >= cfg["min_outside_bars"] and outside_side is not None:
                # ── Filter 1: signal candle size ──────────────────────
                candle_range_pct = (high - low) / close if close > 0 else 1.0
                max_pct = cfg.get("max_signal_candle_pct")
                candle_ok = (max_pct is None) or (candle_range_pct <= max_pct)

                if candle_ok:
                    direction_filter = cfg.get("direction", "both")

                    # ── Filter 2: 1-hour EMA trend direction ──────────────
                    # For shorts: close must be BELOW 1-hour 50 EMA (downtrend)
                    # For longs : close must be ABOVE 1-hour 50 EMA (uptrend)
                    ema_val = bar.get(ema_col, np.nan) if ema_col else np.nan
                    ema_filter_on = cfg.get("ema_trend_filter", False)

                    if outside_side == "below" and direction_filter in ("both", "long"):
                        ema_ok = (
                            not ema_filter_on or
                            (not pd.isna(ema_val) and close > ema_val)
                        )
                        if ema_ok:
                            signal = _Signal(
                                direction    = "long",
                                entry_price  = high,
                                stop_loss    = low,
                                signal_time  = ts,
                                outside_bars = outside_count,
                            )

                    elif outside_side == "above" and direction_filter in ("both", "short"):
                        # Short: only enter if close is BELOW the 1-hour EMA
                        ema_ok = (
                            not ema_filter_on or
                            (not pd.isna(ema_val) and close < ema_val)
                        )
                        if ema_ok:
                            signal = _Signal(
                                direction    = "short",
                                entry_price  = low,
                                stop_loss    = high,
                                signal_time  = ts,
                                outside_bars = outside_count,
                            )

            # Inside the band — reset outside tracker regardless
            outside_count = 0
            outside_side  = None

    # ── Handle any position still open at end of data ─────────────────────────
    if position is not None and bar_list:
        last_ts, last_bar = bar_list[-1]
        pnl = _pnl(position, last_bar["close"])
        trades.append(_close(position, last_ts, last_bar["close"], "eod", pnl))

    return trades
