# ============================================================
# backtesting/strategy_ema_crossover.py
#
# 5/9 EMA Crossover — 5-Min Intraday Strategy
#
# RULES:
#   1. Compute 5 EMA and 9 EMA on 5-min bars.
#
#   2. Long signal  : 5 EMA crosses ABOVE 9 EMA at bar close
#                     (prev bar: 5 ≤ 9, curr bar: 5 > 9)
#      Long entry   : next bar breaks above signal candle HIGH
#      Long SL      : signal candle LOW
#
#   3. Short signal : 5 EMA crosses BELOW 9 EMA at bar close
#                     (prev bar: 5 ≥ 9, curr bar: 5 < 9)
#      Short entry  : next bar breaks below signal candle LOW
#      Short SL     : signal candle HIGH
#
#   4. Signal expiry: valid for ONE bar only — if the next bar
#                     does not trigger, signal is cancelled.
#
#   5. Exit         : SL hit  OR  close of 15:10 bar (15:15)
#                     (SL check happens before time exit each bar)
#
#   6. Filters      : one trade per symbol per day (no re-entry)
#                     no new entries at/after 15:10 bar
#
#   7. Sizing       : risk_per_trade / SL_distance  (capped by max_position_pct)
#
#   Portfolio cap (applied in runner, not here):
#     max 3 long trades / day  +  max 3 short trades / day
#     → first by entry time across all symbols
#
# Run: python backtesting/run_backtest_ema_crossover.py
# ============================================================

import sys
import logging
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger(__name__)

# ── Default config ─────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "fast_ema":         5,          # fast EMA period
    "slow_ema":         9,          # slow EMA period
    "capital":          1_000_000,
    "risk_per_trade":   1_667,      # ₹10,000 daily risk ÷ 6 slots
    "max_position_pct": 0.20,       # max 20% of capital per position
    "exit_time":        "15:10",    # exit at close of 15:10 bar (15:15)
    "no_entry_after":   "14:45",    # no new signals at/after this time
}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol:      str
    direction:   str            # 'long' or 'short'
    signal_time: pd.Timestamp   # bar where EMA crossover was confirmed
    entry_time:  pd.Timestamp   # bar where breakout triggered entry
    entry_price: float          # signal candle high (long) / low (short)
    stop_loss:   float          # signal candle low  (long) / high (short)
    shares:      int
    exit_time:   Optional[pd.Timestamp] = None
    exit_price:  Optional[float]        = None
    exit_reason: Optional[str]          = None  # stop | stop_same_bar | time_exit | eod
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
class _Signal:
    """Pending entry — waiting for next bar to break signal candle high/low."""
    direction:   str            # 'long' or 'short'
    entry_price: float          # long → signal HIGH; short → signal LOW
    stop_loss:   float          # long → signal LOW;  short → signal HIGH
    signal_time: pd.Timestamp


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


# ── Helpers ────────────────────────────────────────────────────────────────────

def _calc_shares(risk_per_trade: float, entry_price: float,
                 stop_loss: float, max_capital: float) -> int:
    """
    shares = min(
        floor(risk_per_trade / |entry - SL|),   # risk-based
        floor(max_capital    / entry_price)      # capital cap
    )
    Returns 0 if SL distance is zero.
    """
    sl_dist = abs(entry_price - stop_loss)
    if sl_dist <= 0:
        return 0
    risk_shares    = int(risk_per_trade / sl_dist)
    capital_shares = int(max_capital    / entry_price)
    return min(risk_shares, capital_shares)


def _pnl(pos: _Position, exit_price: float) -> float:
    if pos.direction == "long":
        return (exit_price - pos.entry_price) * pos.shares
    else:
        return (pos.entry_price - exit_price) * pos.shares


def _close_pos(pos: _Position, exit_time, exit_price, reason) -> Trade:
    return Trade(
        symbol       = pos.symbol,
        direction    = pos.direction,
        signal_time  = pos.signal_time,
        entry_time   = pos.entry_time,
        entry_price  = pos.entry_price,
        stop_loss    = pos.stop_loss,
        shares       = pos.shares,
        exit_time    = exit_time,
        exit_price   = exit_price,
        exit_reason  = reason,
        pnl          = round(_pnl(pos, exit_price), 2),
    )


# ── Core strategy engine ───────────────────────────────────────────────────────

def run_symbol(symbol: str, df_5min: pd.DataFrame, config: dict) -> list[Trade]:
    """
    Run 5/9 EMA Crossover strategy on one symbol.

    Parameters
    ----------
    symbol  : Fyers symbol string, e.g. "NSE:RELIANCE-EQ"
    df_5min : 5-min OHLCV DataFrame from DataLoader (datetime-indexed)
    config  : Strategy config dict (merged with DEFAULT_CONFIG)

    Returns
    -------
    list[Trade] — all completed trades for this symbol
    """
    from backtesting.indicators import add_ema

    cfg = {**DEFAULT_CONFIG, **config}

    # ── 1. Add EMAs ───────────────────────────────────────────────────────────
    df = add_ema(df_5min.copy(), period=cfg["fast_ema"])
    df = add_ema(df,             period=cfg["slow_ema"])
    fast_col = f"ema_{cfg['fast_ema']}"
    slow_col = f"ema_{cfg['slow_ema']}"

    exit_t      = pd.Timestamp(cfg["exit_time"]).time()
    no_entry_t  = pd.Timestamp(cfg["no_entry_after"]).time()
    max_cap     = cfg["capital"] * cfg["max_position_pct"]

    # ── 2. Bar-by-bar simulation ──────────────────────────────────────────────
    trades:   list[Trade]         = []
    signal:   Optional[_Signal]   = None
    position: Optional[_Position] = None

    current_date = None
    traded_today = False
    prev_fast    = None   # previous bar's fast EMA (for crossover detection)
    prev_slow    = None   # previous bar's slow EMA

    bar_list = list(df.iterrows())

    for idx, (ts, bar) in enumerate(bar_list):
        date = ts.date()

        # ── Day boundary reset ────────────────────────────────────────────────
        if date != current_date:
            current_date = date
            traded_today = False
            signal       = None   # cancel any stale signal from prior day
            prev_fast    = None
            prev_slow    = None

        # Skip bars where either EMA hasn't warmed up yet
        fast_val = bar.get(fast_col, np.nan)
        slow_val = bar.get(slow_col, np.nan)
        if pd.isna(fast_val) or pd.isna(slow_val):
            prev_fast = fast_val if not pd.isna(fast_val) else None
            prev_slow = slow_val if not pd.isna(slow_val) else None
            continue

        close = bar["close"]
        high  = bar["high"]
        low   = bar["low"]

        # ════════════════════════════════════════════════════════════════════
        # BRANCH A — Manage open position
        # ════════════════════════════════════════════════════════════════════
        if position is not None:

            if position.direction == "long":
                # SL: bar's low touches or goes below stop loss
                if low <= position.stop_loss:
                    trades.append(_close_pos(position, ts, position.stop_loss, "stop"))
                    position  = None
                    prev_fast = fast_val
                    prev_slow = slow_val
                    continue

                # Time exit: close of 15:10 bar
                if ts.time() >= exit_t:
                    trades.append(_close_pos(position, ts, close, "time_exit"))
                    position  = None
                    prev_fast = fast_val
                    prev_slow = slow_val
                    continue

            else:  # short
                # SL: bar's high touches or goes above stop loss
                if high >= position.stop_loss:
                    trades.append(_close_pos(position, ts, position.stop_loss, "stop"))
                    position  = None
                    prev_fast = fast_val
                    prev_slow = slow_val
                    continue

                # Time exit
                if ts.time() >= exit_t:
                    trades.append(_close_pos(position, ts, close, "time_exit"))
                    position  = None
                    prev_fast = fast_val
                    prev_slow = slow_val
                    continue

            # Still holding
            prev_fast = fast_val
            prev_slow = slow_val
            continue

        # ── Below here: no open position ─────────────────────────────────────

        # ════════════════════════════════════════════════════════════════════
        # BRANCH B — Pending signal: check entry trigger (1-bar validity)
        # ════════════════════════════════════════════════════════════════════
        if signal is not None:
            # Signal is only valid for this one bar — check trigger then expire
            triggered = False
            if   signal.direction == "long"  and high > signal.entry_price:
                triggered = True
            elif signal.direction == "short" and low  < signal.entry_price:
                triggered = True

            if triggered and not traded_today and ts.time() < no_entry_t:
                shares = _calc_shares(
                    cfg["risk_per_trade"],
                    signal.entry_price,
                    signal.stop_loss,
                    max_cap,
                )

                if shares > 0:
                    # Same-bar SL check — entered and immediately stopped
                    same_bar_stop = (
                        (signal.direction == "long"  and low  <= signal.stop_loss) or
                        (signal.direction == "short" and high >= signal.stop_loss)
                    )

                    if same_bar_stop:
                        sl_pnl = -abs(signal.entry_price - signal.stop_loss) * shares
                        trades.append(Trade(
                            symbol       = symbol,
                            direction    = signal.direction,
                            signal_time  = signal.signal_time,
                            entry_time   = ts,
                            entry_price  = signal.entry_price,
                            stop_loss    = signal.stop_loss,
                            shares       = shares,
                            exit_time    = ts,
                            exit_price   = signal.stop_loss,
                            exit_reason  = "stop_same_bar",
                            pnl          = round(sl_pnl, 2),
                        ))
                    else:
                        position = _Position(
                            symbol       = symbol,
                            direction    = signal.direction,
                            signal_time  = signal.signal_time,
                            entry_time   = ts,
                            entry_price  = signal.entry_price,
                            stop_loss    = signal.stop_loss,
                            shares       = shares,
                        )

                    traded_today = True   # one trade per symbol per day

            signal = None   # always expire after one bar
            prev_fast = fast_val
            prev_slow = slow_val
            continue

        # ════════════════════════════════════════════════════════════════════
        # BRANCH C — No position, no pending signal: detect EMA crossover
        # ════════════════════════════════════════════════════════════════════

        # Skip if already traded today, past no-entry cutoff, or no time left
        # (signal needs the NEXT bar to trigger, so stop at no_entry_after)
        if traded_today or ts.time() >= no_entry_t:
            prev_fast = fast_val
            prev_slow = slow_val
            continue

        # Detect crossover (need previous bar's EMA values)
        if prev_fast is not None and prev_slow is not None:
            cross_up   = (prev_fast <= prev_slow) and (fast_val > slow_val)
            cross_down = (prev_fast >= prev_slow) and (fast_val < slow_val)

            if cross_up:
                signal = _Signal(
                    direction    = "long",
                    entry_price  = high,   # break above signal candle HIGH → long entry
                    stop_loss    = low,    # signal candle LOW → long SL
                    signal_time  = ts,
                )
            elif cross_down:
                signal = _Signal(
                    direction    = "short",
                    entry_price  = low,    # break below signal candle LOW → short entry
                    stop_loss    = high,   # signal candle HIGH → short SL
                    signal_time  = ts,
                )

        prev_fast = fast_val
        prev_slow = slow_val

    # ── Handle any position still open at end of data ─────────────────────────
    if position is not None and bar_list:
        last_ts, last_bar = bar_list[-1]
        trades.append(_close_pos(position, last_ts, last_bar["close"], "eod"))

    return trades
