"""Supertrend credit-spread engine — full-signal version.

Difference from strategy.py, and the whole point of this file:

    v1:  dte = cal.dte(date)
         if dte < 4: continue          <- 72% of signals discarded here

    v2:  expiry, dte, n = cal.tradeable_expiry(date, min_dte=4)
         # front week too close? trade the NEXT expiry instead of skipping

v1 could only ever use the front-week contract because that is all the local
dataset contains. Breeze serves every expiry, so a signal firing at 0-3 DTE now
opens in the following week (7-10 DTE) rather than being thrown away.

Everything else — Supertrend flips, OTM-1 short strike, long leg at <50% of the
short premium, stop-loss at the credit, expiry backstop — is unchanged, so the
comparison against v1 is like-for-like.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from datetime import time as dtime

import pandas as pd

STRIKE_STEP = 50
LOT_SIZE = 75
LONG_LEG_PREMIUM_RATIO = 0.5
MAX_STRIKE_STEPS = 10
MIN_DTE_TO_ENTER = 4


def round_to_atm(spot: float) -> int:
    return int(round(spot / STRIKE_STEP) * STRIKE_STEP)


def find_legs(loader, date_str, expiry, entry_time, direction, atm_strike):
    """Short = OTM-1. Long = first strike further out priced under half the short."""
    option_type = "PUT" if direction == 1 else "CALL"
    step_sign = -1 if option_type == "PUT" else 1

    short_strike = atm_strike + step_sign * STRIKE_STEP
    short_price = loader.price_at(date_str, expiry, short_strike, option_type, entry_time)
    if short_price is None or short_price <= 0:
        return None

    long_strike = long_price = None
    farthest_strike = farthest_price = None
    for k in range(2, MAX_STRIKE_STEPS + 1):
        candidate = atm_strike + step_sign * k * STRIKE_STEP
        p = loader.price_at(date_str, expiry, candidate, option_type, entry_time)
        if p is None:
            break
        farthest_strike, farthest_price = candidate, p
        if p < LONG_LEG_PREMIUM_RATIO * short_price:
            long_strike, long_price = candidate, p
            break

    used_fallback_hedge = False
    if long_strike is None:
        if farthest_strike is None or farthest_strike == short_strike:
            return None
        long_strike, long_price = farthest_strike, farthest_price
        used_fallback_hedge = True

    return {
        "option_type": option_type,
        "short_strike": short_strike, "short_price": short_price,
        "long_strike": long_strike, "long_price": long_price,
        "used_fallback_hedge": used_fallback_hedge,
    }


def open_trade(loader, cal, entry_time, direction, lot_size=LOT_SIZE):
    """Open a spread, rolling to a later expiry when the front week is too close."""
    date_str = entry_time.strftime("%Y-%m-%d")

    expiry, dte, expiry_n = cal.tradeable_expiry(date_str, MIN_DTE_TO_ENTER)
    if expiry is None:
        return None, "no_tradeable_expiry"

    spot = loader.spot_at(date_str, entry_time)
    if spot is None:
        return None, "no_spot"
    atm = round_to_atm(spot)

    legs = find_legs(loader, date_str, expiry, entry_time, direction, atm)
    if legs is None:
        return None, "no_legs"

    net_credit_ps = legs["short_price"] - legs["long_price"]
    if net_credit_ps <= 0:
        return None, "no_credit"

    return {
        "entry_time": entry_time, "direction": direction,
        "option_type": legs["option_type"], "atm_strike": atm,
        "short_strike": legs["short_strike"], "long_strike": legs["long_strike"],
        "short_entry_price": legs["short_price"], "long_entry_price": legs["long_price"],
        "net_credit_per_share": net_credit_ps,
        "net_credit_rupees": net_credit_ps * lot_size,
        "expiry_date": expiry, "dte_at_entry": dte, "expiry_n": expiry_n,
        "spot_at_entry": spot, "used_fallback_hedge": legs["used_fallback_hedge"],
        "rolled_to_next_expiry": expiry_n > 0,
    }, None


def simulate_until_exit(loader, trade, flip_events, flip_ptr, available_dates,
                        lot_size=LOT_SIZE, use_stop_loss=True):
    entry_time = trade["entry_time"]
    expiry = trade["expiry_date"]
    short_strike, long_strike = trade["short_strike"], trade["long_strike"]
    option_type = trade["option_type"]
    net_credit_ps = trade["net_credit_per_share"]

    next_flip_time = flip_events[flip_ptr][0] if flip_ptr < len(flip_events) else None

    entry_date_str = entry_time.strftime("%Y-%m-%d")
    expiry_date_str = pd.Timestamp(expiry).strftime("%Y-%m-%d")

    idx_start = available_dates.index(entry_date_str)
    day_range = [d for d in available_dates[idx_start:] if d <= expiry_date_str]

    width = abs(long_strike - short_strike)
    max_loss_rupees = -(width - net_credit_ps) * lot_size
    max_profit_rupees = net_credit_ps * lot_size
    stop_level = -net_credit_ps * lot_size

    def resolve_missing(current_spot):
        """No prices for either leg — settle by moneyness."""
        if option_type == "PUT":
            itm = current_spot < short_strike
        else:
            itm = current_spot > short_strike
        return ((max_loss_rupees, "data_gap_adverse") if itm
                else (max_profit_rupees, "data_gap_favorable"))

    last_spot = trade["spot_at_entry"]
    trade.setdefault("approx_used", False)

    for date_str in day_range:
        day_spot = loader.spot_series_day(date_str)
        if day_spot.empty:
            continue                      # vendor blackout — hold through it
        last_spot = float(day_spot.iloc[0])

        short_s = loader.leg_series(date_str, expiry, short_strike, option_type)
        long_s = loader.leg_series(date_str, expiry, long_strike, option_type)

        if short_s.empty and long_s.empty:
            pnl, reason = resolve_missing(last_spot)
            trade.update(exit_time=pd.Timestamp(date_str + " 09:15:00"),
                         exit_reason=reason, pnl_rupees=pnl,
                         short_exit_price=None, long_exit_price=None, approx_used=True)
            return trade, flip_ptr

        if short_s.empty or long_s.empty:
            # Reconstruct the missing leg from intrinsic value.
            trade["approx_used"] = True
            if option_type == "PUT":
                def intrinsic(k, sp): return (k - sp).clip(lower=0)
            else:
                def intrinsic(k, sp): return (sp - k).clip(lower=0)
            if not short_s.empty:
                long_s = intrinsic(long_strike, day_spot.reindex(short_s.index).ffill())
            else:
                short_s = intrinsic(short_strike, day_spot.reindex(long_s.index).ffill())

        combined = pd.concat([short_s.rename("short"), long_s.rename("long")],
                             axis=1).dropna()
        if date_str == entry_date_str:
            combined = combined[combined.index >= entry_time]
        if date_str == expiry_date_str:
            combined = combined[combined.index.time <= dtime(15, 15)]
        if combined.empty:
            if date_str == expiry_date_str:
                pnl, reason = resolve_missing(last_spot)
                trade.update(exit_time=pd.Timestamp(date_str + " 15:15:00"),
                             exit_reason=reason, pnl_rupees=pnl,
                             short_exit_price=None, long_exit_price=None, approx_used=True)
                return trade, flip_ptr
            continue

        combined["pnl"] = (net_credit_ps - (combined["short"] - combined["long"])) * lot_size

        for ts, r in combined.iterrows():
            if next_flip_time is not None and ts >= next_flip_time:
                trade.update(exit_time=next_flip_time, exit_reason="flip",
                             pnl_rupees=float(r["pnl"]),
                             short_exit_price=float(r["short"]),
                             long_exit_price=float(r["long"]))
                return trade, flip_ptr
            if use_stop_loss and r["pnl"] <= stop_level:
                trade.update(exit_time=ts, exit_reason="stop_loss",
                             pnl_rupees=float(r["pnl"]),
                             short_exit_price=float(r["short"]),
                             long_exit_price=float(r["long"]))
                return trade, flip_ptr

        if date_str == expiry_date_str:
            last_ts, last_r = combined.index[-1], combined.iloc[-1]
            trade.update(exit_time=last_ts, exit_reason="expiry_backstop",
                         pnl_rupees=float(last_r["pnl"]),
                         short_exit_price=float(last_r["short"]),
                         long_exit_price=float(last_r["long"]))
            return trade, flip_ptr

    # Every remaining day was a blackout — settle on the last spot we saw.
    pnl, reason = resolve_missing(last_spot)
    trade.update(exit_time=pd.Timestamp(expiry_date_str + " 15:15:00"),
                 exit_reason=reason + "_forced", pnl_rupees=pnl,
                 short_exit_price=None, long_exit_price=None, approx_used=True)
    return trade, flip_ptr


def next_trading_day(date_str, available_dates):
    idx = available_dates.index(date_str)
    return available_dates[idx + 1] if idx + 1 < len(available_dates) else None


def run_backtest(loader, cal, flip_events, available_dates, roll_forward: bool,
                 lot_size=LOT_SIZE, max_trades=20000, progress_every=25,
                 use_stop_loss=True):
    trades, skipped = [], []
    flip_ptr, pos = 0, None
    n = len(flip_events)

    while flip_ptr < n or pos is not None:
        if len(trades) > max_trades:
            print("WARNING: max_trades cap hit")
            break

        if pos is None:
            if flip_ptr >= n:
                break
            entry_time, direction = flip_events[flip_ptr]
            flip_ptr += 1
            pos, why = open_trade(loader, cal, entry_time, direction, lot_size)
            if pos is None:
                skipped.append({"time": entry_time, "direction": direction,
                                "reason": why})
            continue

        pos_direction = pos["direction"]
        closed, flip_ptr = simulate_until_exit(loader, pos, flip_events, flip_ptr,
                                               available_dates, lot_size,
                                               use_stop_loss=use_stop_loss)
        trades.append(closed)
        if progress_every and len(trades) % progress_every == 0:
            print(f"    ... {len(trades)} trades closed "
                  f"(flip {flip_ptr}/{n}, skipped {len(skipped)})")
        reason = closed["exit_reason"]

        if reason == "flip":
            entry_time, direction = flip_events[flip_ptr]
            flip_ptr += 1
            pos, why = open_trade(loader, cal, entry_time, direction, lot_size)
            if pos is None:
                skipped.append({"time": entry_time, "direction": direction,
                                "reason": why})
        elif reason == "expiry_backstop" and roll_forward:
            nxt = next_trading_day(closed["exit_time"].strftime("%Y-%m-%d"),
                                   available_dates)
            pos = None
            if nxt is not None:
                day_spot = loader.spot_series_day(nxt)
                if not day_spot.empty:
                    pos, _ = open_trade(loader, cal, day_spot.index[0],
                                        pos_direction, lot_size)
        else:
            pos = None

    return trades, skipped
