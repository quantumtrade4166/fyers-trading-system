"""
Intraday naked ATM option-selling driven by daily pivots + 5-min Supertrend.

Base rules:
  Levels:  standard floor pivots from the PREVIOUS session's H/L/C
  Trend:   Supertrend(7, 3) on continuous 5-min Nifty spot bars
  Bullish: close > Supertrend AND close > R1  -> SELL naked ATM PUT
  Bearish: close < Supertrend AND close < S1  -> SELL naked ATM CALL
  Exit:    close crosses back through Supertrend, else square-off at 15:15
  Limits:  max 2 entries/day, one position at a time, intraday only

Optional entry filters (all default to OFF = original behaviour):
  cooldown_bars : after an exit, block new entries for N bars (anti-whipsaw)
  fresh_cross   : enter only when the condition turns TRUE (a real crossover),
                  not on any bar where it merely happens to already be true
  st_buffer     : require close to clear the Supertrend line by this many
                  points, so a marginal poke over the line doesn't trigger

Fills use the option's OPEN at the signal bar's label timestamp. No lookahead.
"""
import sys
from datetime import time as dtime

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

STRIKE_STEP = 50
LOT_SIZE = 75
MAX_ENTRIES_PER_DAY = 2
SQUAREOFF_TIME = dtime(15, 15)
LAST_ENTRY_TIME = dtime(15, 10)


def round_to_atm(spot: float) -> int:
    return int(round(spot / STRIKE_STEP) * STRIKE_STEP)


def _price_at(open_pivot, ts, strike, option_type):
    col = (strike, option_type)
    if col not in open_pivot.columns or ts not in open_pivot.index:
        return None
    val = open_pivot.at[ts, col]
    if pd.isna(val):
        return None
    return float(val)


def _intrinsic(strike, spot, option_type):
    if option_type == "PUT":
        return max(0.0, strike - spot)
    return max(0.0, spot - strike)


def run_day(loader, date_str, day_bars, r1, s1, dir_col, st_col, lot_size=LOT_SIZE,
            cooldown_bars=0, fresh_cross=False, st_buffer=0.0,
            exit_confirm_bars=1, use_st_exit=True, stop_mult=None,
            min_premium=0.0, min_premium_pct=0.0):
    open_pivot = loader.get_day_pivot(date_str, field="open")
    if open_pivot.empty:
        return []
    high_pivot = loader.get_day_pivot(date_str, field="high") if stop_mult else None

    trades = []
    entries_used = 0
    pos = None
    cooldown_until = -1
    prev_bull = None
    prev_bear = None
    adverse_streak = 0
    prev_ts = None

    for i, (ts, bar) in enumerate(day_bars.iterrows()):
        bar_time = ts.time()
        direction = int(bar[dir_col])
        close = float(bar["close"])
        st_line = float(bar[st_col])

        bull = (direction == 1) and (close > r1) and (close > st_line + st_buffer)
        bear = (direction == -1) and (close < s1) and (close < st_line - st_buffer)

        # ── manage an open position ──────────────────────────────────────
        if pos is not None:
            adverse = (pos["option_type"] == "PUT" and direction == -1) or                       (pos["option_type"] == "CALL" and direction == 1)
            adverse_streak = adverse_streak + 1 if adverse else 0
            forced = bar_time >= SQUAREOFF_TIME

            stop_hit = False
            if stop_mult and pos.get("stop_level") and high_pivot is not None:
                col = (pos["strike"], pos["option_type"])
                if col in high_pivot.columns and prev_ts is not None:
                    win = high_pivot.loc[(high_pivot.index > prev_ts) & (high_pivot.index <= ts), col].dropna()
                    if len(win) and float(win.max()) >= pos["stop_level"]:
                        stop_hit = True

            st_triggered = use_st_exit and adverse_streak >= exit_confirm_bars

            if stop_hit or st_triggered or forced:
                if stop_hit:
                    exit_price, approx, reason = pos["stop_level"], False, "premium_stop"
                else:
                    exit_price = _price_at(open_pivot, ts, pos["strike"], pos["option_type"])
                    approx = False
                    if exit_price is None:
                        exit_price = _intrinsic(pos["strike"], close, pos["option_type"])
                        approx = True
                    reason = "squareoff_1515" if forced else "supertrend_flip"
                trades.append({
                    **pos, "exit_time": ts, "exit_price": exit_price, "exit_reason": reason,
                    "spot_at_exit": close, "exit_approx": approx,
                    "pnl_rupees": (pos["entry_price"] - exit_price) * lot_size,
                })
                pos = None
                adverse_streak = 0
                cooldown_until = i + cooldown_bars - 1
            else:
                prev_bull, prev_bear = bull, bear
                prev_ts = ts
                continue

        # ── look for a fresh entry ───────────────────────────────────────
        if entries_used < MAX_ENTRIES_PER_DAY and bar_time <= LAST_ENTRY_TIME and i > cooldown_until:
            take_bull = bull and (not fresh_cross or prev_bull is False)
            take_bear = bear and (not fresh_cross or prev_bear is False)
            option_type = "PUT" if take_bull else ("CALL" if take_bear else None)
            if option_type is not None:
                strike = round_to_atm(close)
                entry_price = _price_at(open_pivot, ts, strike, option_type)
                floor = max(min_premium, min_premium_pct / 100.0 * close)
                if entry_price is not None and entry_price > 0 and entry_price >= floor:
                    entries_used += 1
                    pos = {
                        "date": date_str, "entry_time": ts, "option_type": option_type,
                        "side": "short_put" if option_type == "PUT" else "short_call",
                        "strike": strike, "entry_price": entry_price,
                        "spot_at_entry": close, "r1": r1, "s1": s1, "entry_no": entries_used,
                        "stop_level": entry_price * stop_mult if stop_mult else None,
                    }

        prev_bull, prev_bear = bull, bear
        prev_ts = ts

    # ── safety: still open after the last bar ────────────────────────────
    if pos is not None:
        ts = day_bars.index[-1]
        close = float(day_bars.iloc[-1]["close"])
        exit_price = _price_at(open_pivot, ts, pos["strike"], pos["option_type"])
        approx = False
        if exit_price is None:
            exit_price = _intrinsic(pos["strike"], close, pos["option_type"])
            approx = True
        trades.append({
            **pos, "exit_time": ts, "exit_price": exit_price, "exit_reason": "eod_last_bar",
            "spot_at_exit": close, "exit_approx": approx,
            "pnl_rupees": (pos["entry_price"] - exit_price) * lot_size,
        })

    return trades


def run_backtest(loader, bars, pivots, dir_col, st_col, lot_size=LOT_SIZE,
                 cooldown_bars=0, fresh_cross=False, st_buffer=0.0,
                 exit_confirm_bars=1, use_st_exit=True, stop_mult=None,
                 min_premium=0.0, min_premium_pct=0.0,
                 date_from=None, date_to=None):
    bars = bars[bars[dir_col] != 0]
    by_day = {d: g for d, g in bars.groupby(bars.index.normalize().strftime("%Y-%m-%d"), sort=False)}

    all_trades = []
    for date_str, row in pivots.iterrows():
        if date_from and date_str < date_from:
            continue
        if date_to and date_str > date_to:
            continue
        day_bars = by_day.get(date_str)
        if day_bars is None or day_bars.empty:
            continue
        all_trades.extend(run_day(
            loader, date_str, day_bars, float(row["r1"]), float(row["s1"]),
            dir_col, st_col, lot_size,
            cooldown_bars=cooldown_bars, fresh_cross=fresh_cross, st_buffer=st_buffer,
            exit_confirm_bars=exit_confirm_bars, use_st_exit=use_st_exit, stop_mult=stop_mult,
            min_premium=min_premium, min_premium_pct=min_premium_pct,
        ))
    return all_trades
