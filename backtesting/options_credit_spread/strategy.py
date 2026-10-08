"""
Supertrend (1H) credit-spread strategy engine.

Rules (as specified):
  Entry:  1H Supertrend flips green -> sell OTM-1 Put + buy the first further
          strike whose premium < 50% of the short premium (bull put spread).
          Flips red -> same construction on the Call side (bear call spread).
          Only enters if DTE (of the current front-week expiry) >= 4.
  Exit (first of):
    - the next Supertrend flip (opposite direction)
    - running loss reaches the entry credit (stop-loss)
    - 3:15 PM on the position's own expiry day, if still open (backstop)
  Data-gap fallback: if either leg's strike has drifted outside the day's
  ATM+/-10 window, close immediately assuming max-loss value (width - credit),
  logged as reason 'data_gap'.
  Post-exit behavior:
    - after a flip exit -> immediately open the opposite trade (if DTE ok)
    - after a stop-loss exit -> stay flat until the next flip
    - after an expiry backstop exit -> controlled by `roll_forward`:
        True  = re-enter same direction next trading day (new front week), if DTE ok
        False = stay flat until the next flip
"""
import sys
from datetime import time as dtime
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

STRIKE_STEP = 50
LOT_SIZE = 75
LONG_LEG_PREMIUM_RATIO = 0.5
MAX_STRIKE_STEPS = 10
MIN_DTE_TO_ENTER = 4


def round_to_atm(spot: float) -> int:
    return int(round(spot / STRIKE_STEP) * STRIKE_STEP)


def find_short_and_long_strike(loader, date_str, entry_time, direction, atm_strike):
    option_type = "PUT" if direction == 1 else "CALL"
    step_sign = -1 if option_type == "PUT" else 1  # OTM direction

    chain = loader.get_day_chain(date_str)
    at_time = chain[chain["datetime"] == entry_time]
    if at_time.empty:
        return None

    def price_at(strike):
        row = at_time[(at_time["strike_price"] == strike) & (at_time["option_type"] == option_type)]
        if row.empty:
            return None
        return float(row["close"].iloc[0])

    short_strike = atm_strike + step_sign * 1 * STRIKE_STEP
    short_price = price_at(short_strike)
    if short_price is None or short_price <= 0:
        return None

    long_strike, long_price = None, None
    farthest_strike, farthest_price = None, None
    for k in range(2, MAX_STRIKE_STEPS + 1):
        candidate = atm_strike + step_sign * k * STRIKE_STEP
        p = price_at(candidate)
        if p is None:
            break
        farthest_strike, farthest_price = candidate, p
        if p < LONG_LEG_PREMIUM_RATIO * short_price:
            long_strike, long_price = candidate, p
            break

    used_fallback_hedge = False
    if long_strike is None:
        if farthest_strike is None or farthest_strike == short_strike:
            return None  # not even one further strike available -- can't build a spread
        long_strike, long_price = farthest_strike, farthest_price
        used_fallback_hedge = True

    return {
        "option_type": option_type,
        "short_strike": short_strike, "short_price": short_price,
        "long_strike": long_strike, "long_price": long_price,
        "used_fallback_hedge": used_fallback_hedge,
    }


def open_trade(loader, expiry_cal, entry_time, direction, lot_size=LOT_SIZE):
    date_str = entry_time.strftime("%Y-%m-%d")
    chain = loader.get_day_chain(date_str)
    if chain.empty:
        return None
    row = chain[chain["datetime"] == entry_time]
    if row.empty:
        return None
    spot = float(row["spot"].iloc[0])
    atm = round_to_atm(spot)

    legs = find_short_and_long_strike(loader, date_str, entry_time, direction, atm)
    if legs is None:
        return None

    net_credit_ps = legs["short_price"] - legs["long_price"]
    if net_credit_ps <= 0:
        return None

    expiry_date = expiry_cal.expiry_on_or_after(date_str)
    if expiry_date is None:
        return None
    dte = expiry_cal.dte(date_str)

    return {
        "entry_time": entry_time, "direction": direction, "option_type": legs["option_type"],
        "atm_strike": atm, "short_strike": legs["short_strike"], "long_strike": legs["long_strike"],
        "short_entry_price": legs["short_price"], "long_entry_price": legs["long_price"],
        "net_credit_per_share": net_credit_ps, "net_credit_rupees": net_credit_ps * lot_size,
        "expiry_date": expiry_date, "dte_at_entry": dte, "spot_at_entry": spot,
        "used_fallback_hedge": legs["used_fallback_hedge"],
    }


def simulate_until_exit(loader, trade, flip_events, flip_ptr, available_dates, lot_size=LOT_SIZE):
    entry_time = trade["entry_time"]
    expiry_date = trade["expiry_date"]
    short_strike, long_strike, option_type = trade["short_strike"], trade["long_strike"], trade["option_type"]
    net_credit_ps = trade["net_credit_per_share"]

    next_flip_time = flip_events[flip_ptr][0] if flip_ptr < len(flip_events) else None

    entry_date_str = entry_time.strftime("%Y-%m-%d")
    expiry_date_str = expiry_date.strftime("%Y-%m-%d")

    idx_start = available_dates.index(entry_date_str)
    day_range = [d for d in available_dates[idx_start:] if d <= expiry_date_str]

    width = abs(long_strike - short_strike)
    max_loss_rupees = -(width - net_credit_ps) * lot_size
    max_profit_rupees = net_credit_ps * lot_size

    def _intrinsic(strike, spot_series):
        """Deep-moneyness approximation for a leg that has drifted off-grid:
        deep OTM -> ~0, deep ITM -> ~intrinsic value (time value negligible)."""
        if option_type == "PUT":
            return (strike - spot_series).clip(lower=0)
        return (spot_series - strike).clip(lower=0)

    def _resolve_both_off_grid(current_spot):
        """Both legs outside the window -- can't price either, only tell which
        side we're on: deep OTM (favorable -> ~max profit) vs deep ITM
        (adverse -> ~max loss)."""
        if option_type == "PUT":
            itm = current_spot < short_strike
        else:
            itm = current_spot > short_strike
        return (max_loss_rupees, "data_gap_adverse") if itm else (max_profit_rupees, "data_gap_favorable")

    last_seen_spot = trade["spot_at_entry"]
    trade.setdefault("approx_used", False)

    for date_str in day_range:
        chain = loader.get_day_chain(date_str)
        if chain.empty:
            # genuine vendor data blackout (e.g. the Dec-Jan gap) -- no info at
            # all this day, not even spot. Hold state through it silently.
            continue
        last_seen_spot = float(chain["spot"].iloc[0])

        short_present = loader.has_strike(date_str, short_strike, option_type)
        long_present = loader.has_strike(date_str, long_strike, option_type)

        if not short_present and not long_present:
            pnl, reason = _resolve_both_off_grid(last_seen_spot)
            trade.update(exit_time=pd.Timestamp(date_str + " 09:15:00"), exit_reason=reason,
                         pnl_rupees=pnl, short_exit_price=None, long_exit_price=None, approx_used=True)
            return trade, flip_ptr

        if short_present and long_present:
            short_s = loader.leg_series(date_str, short_strike, option_type)
            long_s = loader.leg_series(date_str, long_strike, option_type)
            combined = pd.concat([short_s.rename("short"), long_s.rename("long")], axis=1).dropna()
        else:
            # exactly one leg off-grid -- price the leg we have, reconstruct the
            # missing one from intrinsic value. On a favorable drift the short
            # leg is usually still priced and only the far long leg is missing.
            trade["approx_used"] = True
            spot_day = loader.spot_series_day(date_str)
            if short_present:
                short_s = loader.leg_series(date_str, short_strike, option_type)
                long_s = _intrinsic(long_strike, spot_day.reindex(short_s.index))
                combined = pd.concat([short_s.rename("short"), long_s.rename("long")], axis=1).dropna()
            else:
                long_s = loader.leg_series(date_str, long_strike, option_type)
                short_s = _intrinsic(short_strike, spot_day.reindex(long_s.index))
                combined = pd.concat([short_s.rename("short"), long_s.rename("long")], axis=1).dropna()

        if date_str == entry_date_str:
            combined = combined[combined.index >= entry_time]
        if date_str == expiry_date_str:
            combined = combined[combined.index.time <= dtime(15, 15)]

        if combined.empty:
            if date_str == expiry_date_str:
                pnl, reason = _resolve_both_off_grid(last_seen_spot)
                trade.update(exit_time=pd.Timestamp(date_str + " 15:15:00"), exit_reason=reason,
                             pnl_rupees=pnl, short_exit_price=None, long_exit_price=None, approx_used=True)
                return trade, flip_ptr
            continue

        combined["pnl"] = (net_credit_ps - (combined["short"] - combined["long"])) * lot_size

        for ts, r in combined.iterrows():
            if next_flip_time is not None and ts >= next_flip_time:
                trade.update(exit_time=next_flip_time, exit_reason="flip",
                             pnl_rupees=float(r["pnl"]), short_exit_price=float(r["short"]),
                             long_exit_price=float(r["long"]))
                return trade, flip_ptr
            if r["pnl"] <= -net_credit_ps * lot_size:
                trade.update(exit_time=ts, exit_reason="stop_loss",
                             pnl_rupees=float(r["pnl"]), short_exit_price=float(r["short"]),
                             long_exit_price=float(r["long"]))
                return trade, flip_ptr

        if date_str == expiry_date_str:
            last_ts = combined.index[-1]
            last_r = combined.iloc[-1]
            trade.update(exit_time=last_ts, exit_reason="expiry_backstop",
                         pnl_rupees=float(last_r["pnl"]), short_exit_price=float(last_r["short"]),
                         long_exit_price=float(last_r["long"]))
            return trade, flip_ptr

    # Extreme edge case: every remaining day (including expiry day) was a total
    # vendor blackout -- resolve using the last spot we ever saw for this trade.
    pnl, reason = _resolve_off_grid(last_seen_spot, expiry_date_str, "15:15:00")
    trade.update(exit_time=pd.Timestamp(expiry_date_str + " 15:15:00"), exit_reason=reason + "_forced",
                 pnl_rupees=pnl, short_exit_price=None, long_exit_price=None)
    return trade, flip_ptr


def next_trading_day(date_str, available_dates):
    idx = available_dates.index(date_str)
    if idx + 1 < len(available_dates):
        return available_dates[idx + 1]
    return None


def run_backtest(loader, expiry_cal, flip_events, available_dates, roll_forward: bool, lot_size=LOT_SIZE,
                  max_trades=20000):
    trades = []
    flip_ptr = 0
    n = len(flip_events)
    pos = None

    while flip_ptr < n or pos is not None:
        if len(trades) > max_trades:
            print("WARNING: max_trades safety cap hit, stopping early")
            break

        if pos is None:
            if flip_ptr >= n:
                break
            entry_time, direction = flip_events[flip_ptr]
            flip_ptr += 1
            date_str = entry_time.strftime("%Y-%m-%d")
            dte = expiry_cal.dte(date_str)
            if dte is None or dte < MIN_DTE_TO_ENTER:
                continue
            pos = open_trade(loader, expiry_cal, entry_time, direction, lot_size)
            continue

        pos_direction = pos["direction"]
        closed_trade, flip_ptr = simulate_until_exit(loader, pos, flip_events, flip_ptr, available_dates, lot_size)
        trades.append(closed_trade)
        reason = closed_trade["exit_reason"]

        if reason == "flip":
            entry_time, direction = flip_events[flip_ptr]
            flip_ptr += 1
            date_str = entry_time.strftime("%Y-%m-%d")
            dte = expiry_cal.dte(date_str)
            pos = open_trade(loader, expiry_cal, entry_time, direction, lot_size) if (dte is not None and dte >= MIN_DTE_TO_ENTER) else None

        elif reason in ("expiry_backstop", "data_gap") and roll_forward and reason == "expiry_backstop":
            nxt = next_trading_day(closed_trade["exit_time"].strftime("%Y-%m-%d"), available_dates)
            pos = None
            if nxt is not None:
                dte = expiry_cal.dte(nxt)
                if dte is not None and dte >= MIN_DTE_TO_ENTER:
                    chain = loader.get_day_chain(nxt)
                    if not chain.empty:
                        roll_entry_time = chain["datetime"].min()
                        pos = open_trade(loader, expiry_cal, roll_entry_time, pos_direction, lot_size)
        else:
            pos = None  # stop_loss, or expiry_backstop/data_gap with roll_forward=False

    return trades
