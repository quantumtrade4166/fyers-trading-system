# ============================================================
# backtesting/strategy_5ema_short.py
#
# 5 EMA 5-Min Short — Gap-Up Reversal Strategy  (V2)
#
# RULES:
#   1. Gap-up filter  : today's open > yesterday's close by ≥ 0.8%
#                       (checked once at the first bar of each day)
#
#   2. Bar 1 (09:15)  : MUST be GREEN (close > open) AND close > 5 EMA
#   3. Bar 2 (09:20)  : MUST be GREEN (close > open) AND close > 5 EMA
#      → If either bar 1 or bar 2 fails, setup is dead for the day.
#
#   4. Bar 3 onward   : close > 5 EMA  (green or red, any colour is OK)
#      → Continuous streak required from bar 1. If any bar from bar 3+
#        closes below EMA the setup is dead (in practice the entry trigger
#        fires first for a tight signal candle).
#
#   5. Signal candle  : first bar from bar 3+ where streak is intact AND
#                       candle range ≤ 0.5%  [ (high-low)/close ≤ 0.005 ]
#                       entry trigger = signal candle LOW
#                       stop loss     = signal candle HIGH
#
#   6. Entry          : SHORT when next bar's low ≤ signal candle LOW
#
#   7. Stop loss      : signal candle HIGH (fixed, no trail)
#
#   8. Exit           : stop loss hit  OR  15:00 bar close (time exit)
#                       (stop check happens BEFORE time exit each bar)
#
#   9. Filters        : no new entries AT or AFTER 10:00 AM
#                       one trade per stock per day
#
# Run:  python backtesting/run_backtest_5ema.py
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
    "ema_period":             5,        # 5 EMA on 5-min bars
    "gap_up_pct":             0.015,    # 1.5% minimum gap-up
    "max_trigger_candle_pct": 0.005,    # signal candle range ≤ 0.5% of close
    "signal_vol_ratio":       0.70,     # signal candle volume < 70% of bar-1 volume
    "capital":                1_000_000,
    "risk_per_trade":         2_000,    # ₹2,000 per trade
    "max_position_pct":       0.20,     # max 20% of capital in one position
    "exit_time":              "15:00",  # time-exit — flat by this bar
    "no_entry_after":         "10:00",  # no new entries at/after this time
}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol:      str
    direction:   str           # always 'short'
    signal_time: pd.Timestamp  # bar that formed the signal candle
    entry_time:  pd.Timestamp
    entry_price: float         # signal candle low
    stop_loss:   float         # signal candle high
    shares:      int
    exit_time:   Optional[pd.Timestamp] = None
    exit_price:  Optional[float]        = None
    exit_reason: Optional[str]          = None  # stop | stop_same_bar | time_exit | eod
    pnl:         float                  = 0.0
    green_bars:  int                    = 0     # above-EMA streak count at signal

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
    """Pending short entry — waiting for signal candle low to be broken."""
    entry_price: float           # signal candle LOW  → short trigger
    stop_loss:   float           # signal candle HIGH → SL
    signal_time: pd.Timestamp
    streak_bars: int             # above-EMA streak count when signal was set


@dataclass
class _Position:
    """Open short position being managed."""
    symbol:      str
    signal_time: pd.Timestamp
    entry_time:  pd.Timestamp
    entry_price: float
    stop_loss:   float
    shares:      int
    streak_bars: int


# ── Helpers ────────────────────────────────────────────────────────────────────

def _calc_shares(risk_per_trade: float, entry_price: float,
                 stop_loss: float, max_capital: float) -> int:
    """
    shares = min(
        floor(risk_per_trade / SL_distance),  # risk-based
        floor(max_capital    / entry_price)    # capital cap
    )
    Returns 0 if SL distance is zero (degenerate bar → skip).
    """
    sl_dist = abs(entry_price - stop_loss)
    if sl_dist <= 0:
        return 0
    risk_shares    = int(risk_per_trade / sl_dist)
    capital_shares = int(max_capital    / entry_price)
    return min(risk_shares, capital_shares)


def _pnl(pos: _Position, exit_price: float) -> float:
    return (pos.entry_price - exit_price) * pos.shares   # short PnL


def _close(pos: _Position, exit_time, exit_price, reason, pnl) -> Trade:
    return Trade(
        symbol       = pos.symbol,
        direction    = "short",
        signal_time  = pos.signal_time,
        entry_time   = pos.entry_time,
        entry_price  = pos.entry_price,
        stop_loss    = pos.stop_loss,
        shares       = pos.shares,
        exit_time    = exit_time,
        exit_price   = exit_price,
        exit_reason  = reason,
        pnl          = round(pnl, 2),
        green_bars   = pos.streak_bars,
    )


# ── Core strategy engine ───────────────────────────────────────────────────────

def run_symbol(symbol: str, df_5min: pd.DataFrame, config: dict) -> list[Trade]:
    """
    Run 5 EMA Short V2 strategy on one symbol.

    Parameters
    ----------
    symbol  : Fyers symbol string, e.g. "NSE:RELIANCE-EQ"
    df_5min : 5-min OHLCV DataFrame from DataLoader (datetime-indexed)
    config  : Strategy config dict (merged with DEFAULT_CONFIG)

    Returns
    -------
    list[Trade] — all completed trades (entered AND exited)
    """
    from backtesting.indicators import add_ema

    cfg = {**DEFAULT_CONFIG, **config}

    # ── 1. Add 5 EMA on 5-min bars ───────────────────────────────────────────
    df = add_ema(df_5min.copy(), period=cfg["ema_period"])
    ema_col = f"ema_{cfg['ema_period']}"

    exit_t          = pd.Timestamp(cfg["exit_time"]).time()
    no_entry_t      = pd.Timestamp(cfg["no_entry_after"]).time()
    max_cap         = cfg["capital"] * cfg["max_position_pct"]
    gap_thresh      = cfg["gap_up_pct"]
    max_range_pct   = cfg["max_trigger_candle_pct"]
    vol_ratio       = cfg["signal_vol_ratio"]

    # ── 2. Pre-compute previous day's closing price per date ──────────────────
    date_arr   = pd.Series(df.index.date, index=df.index)
    day_groups = df.groupby(date_arr)
    daily_last_close: dict = {}
    for d, grp in day_groups:
        daily_last_close[d] = grp["close"].iloc[-1]

    sorted_dates = sorted(daily_last_close.keys())
    prev_close_map: dict = {}
    for i, d in enumerate(sorted_dates):
        prev_close_map[d] = daily_last_close[sorted_dates[i - 1]] if i > 0 else None

    # ── 3. Bar-by-bar simulation ──────────────────────────────────────────────
    trades:   list[Trade]         = []
    signal:   Optional[_Signal]   = None
    position: Optional[_Position] = None

    # ── Per-day state ─────────────────────────────────────────────────────────
    current_date     = None
    gap_up_ok        = False
    traded_today     = False
    bar_count_today  = 0      # 1-indexed; increments each valid (non-NaN EMA) bar
    first_bar_ok     = False  # bar 1: green AND close > EMA
    second_bar_ok    = False  # bar 2: green AND close > EMA
    above_ema_streak = 0      # continuous above-EMA bars since bar 1
    setup_dead       = False  # True once bar 1/2 fails or streak breaks
    bar1_volume      = 0      # volume of bar 1 (09:15) — used for volume filter

    bar_list = list(df.iterrows())

    for idx, (ts, bar) in enumerate(bar_list):
        date = ts.date()

        # ── Day boundary reset ────────────────────────────────────────────────
        if date != current_date:
            current_date     = date
            bar_count_today  = 0
            first_bar_ok     = False
            second_bar_ok    = False
            above_ema_streak = 0
            setup_dead       = False
            traded_today     = False
            bar1_volume      = 0
            signal           = None   # cancel stale signal from prior day

            # Gap-up filter — compare today's open to yesterday's close
            today_open = bar["open"]
            prev_close = prev_close_map.get(date)
            if prev_close is not None and prev_close > 0:
                gap_pct   = (today_open - prev_close) / prev_close
                gap_up_ok = gap_pct >= gap_thresh
            else:
                gap_up_ok = False   # first day in dataset, skip

        # Skip until EMA has warmed up (first few bars of entire dataset)
        ema_val = bar.get(ema_col, np.nan)
        if pd.isna(ema_val):
            continue

        # Increment bar count for this day (only bars with valid EMA count)
        bar_count_today += 1

        close = bar["close"]
        high  = bar["high"]
        low   = bar["low"]
        open_ = bar["open"]

        above_ema_now = close > ema_val

        # ── Track bar 1 / bar 2 / bar 3+ state ───────────────────────────────
        # This runs every bar so we always know the day's setup state,
        # even while managing a position or watching a pending signal.
        if gap_up_ok and not setup_dead:
            if bar_count_today == 1:
                # Bar 1 must be green AND above EMA
                if close > open_ and above_ema_now:
                    first_bar_ok     = True
                    above_ema_streak = 1
                    bar1_volume      = bar["volume"]   # capture for volume filter
                else:
                    setup_dead = True

            elif bar_count_today == 2:
                # Bar 2 must be green AND above EMA
                if close > open_ and above_ema_now:
                    second_bar_ok    = True
                    above_ema_streak = 2
                else:
                    setup_dead = True

            else:
                # Bar 3+: just needs to be above EMA (continuous streak)
                if above_ema_now:
                    above_ema_streak += 1
                else:
                    setup_dead = True
                    # Cancel any pending signal — streak broken
                    if signal is not None:
                        signal = None

        # ════════════════════════════════════════════════════════════════════
        # BRANCH A — Manage open position
        # (runs regardless of gap_up_ok — must manage what we opened)
        # ════════════════════════════════════════════════════════════════════
        if position is not None:

            # Stop loss FIRST — if bar's high hits SL, exit at SL price
            if high >= position.stop_loss:
                pnl = _pnl(position, position.stop_loss)
                trades.append(_close(position, ts, position.stop_loss, "stop", pnl))
                position = None
                continue

            # Time exit — flat at 15:00 bar close
            if ts.time() >= exit_t:
                pnl = _pnl(position, close)
                trades.append(_close(position, ts, close, "time_exit", pnl))
                position = None
                continue

            continue   # still holding — nothing else to do this bar

        # ── Below here: no open position ─────────────────────────────────────

        # Skip this stock today if gap-up condition not met
        if not gap_up_ok:
            continue

        # ════════════════════════════════════════════════════════════════════
        # BRANCH B — Pending signal: check entry trigger
        # ════════════════════════════════════════════════════════════════════
        if signal is not None:

            # Cancel if at/past the no-entry cutoff
            if ts.time() >= no_entry_t:
                signal = None
                continue

            # Check if bar's low breaks signal candle low → short entry
            if low <= signal.entry_price:
                shares = _calc_shares(
                    cfg["risk_per_trade"],
                    signal.entry_price,
                    signal.stop_loss,
                    max_cap,
                )

                if shares > 0:
                    # Check if stopped in the same bar (entered & stopped immediately)
                    if high >= signal.stop_loss:
                        # Enter + stop in same bar — full SL loss realised
                        pnl = (signal.entry_price - signal.stop_loss) * shares
                        trades.append(Trade(
                            symbol       = symbol,
                            direction    = "short",
                            signal_time  = signal.signal_time,
                            entry_time   = ts,
                            entry_price  = signal.entry_price,
                            stop_loss    = signal.stop_loss,
                            shares       = shares,
                            exit_time    = ts,
                            exit_price   = signal.stop_loss,
                            exit_reason  = "stop_same_bar",
                            pnl          = round(pnl, 2),
                            green_bars   = signal.streak_bars,
                        ))
                    else:
                        position = _Position(
                            symbol       = symbol,
                            signal_time  = signal.signal_time,
                            entry_time   = ts,
                            entry_price  = signal.entry_price,
                            stop_loss    = signal.stop_loss,
                            shares       = shares,
                            streak_bars  = signal.streak_bars,
                        )

                    traded_today = True   # one trade per stock per day

                signal = None   # order consumed (triggered or 0 shares → discard)

            continue   # signal not triggered yet — keep watching

        # ════════════════════════════════════════════════════════════════════
        # BRANCH C — No position, no signal: detect new setup
        # ════════════════════════════════════════════════════════════════════

        # Skip if already traded today, past entry cutoff, or setup is dead
        if traded_today or ts.time() >= no_entry_t or setup_dead:
            continue

        # Need at least bar 3 with intact streak (bars 1 + 2 green+above EMA,
        # bar 3+ above EMA, continuous from bar 1)
        if not (first_bar_ok and second_bar_ok and above_ema_streak >= 3):
            continue

        # Signal candle filters:
        #   1. candle range ≤ max_trigger_candle_pct  (tight candle near EMA)
        #   2. volume < vol_ratio × bar-1 volume      (buying pressure drying up)
        candle_range_pct = (high - low) / close if close > 0 else 999.0
        vol_ok = (bar1_volume == 0) or (bar["volume"] < vol_ratio * bar1_volume)
        if candle_range_pct <= max_range_pct and vol_ok:
            signal = _Signal(
                entry_price = low,
                stop_loss   = high,
                signal_time = ts,
                streak_bars = above_ema_streak,
            )

    # ── Handle any position still open at end of data ─────────────────────────
    if position is not None and bar_list:
        last_ts, last_bar = bar_list[-1]
        pnl = _pnl(position, last_bar["close"])
        trades.append(_close(position, last_ts, last_bar["close"], "eod", pnl))

    return trades
