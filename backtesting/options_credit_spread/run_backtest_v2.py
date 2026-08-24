"""Full-signal Supertrend credit-spread backtest (v2).

v1 discarded every signal firing at 0-3 DTE because the local dataset only holds
the front-week contract — 72% of them. v2 routes those into the next expiry,
pulling prices from Breeze on demand.

Start with a dry run. It walks the whole backtest, records every contract-day it
would need, and spends zero API calls:

    python backtesting/options_credit_spread/run_backtest_v2.py --dry-run

Then fetch + run for real:

    python backtesting/options_credit_spread/run_backtest_v2.py
    python backtesting/options_credit_spread/run_backtest_v2.py --start 2024-01-01
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse

from backtesting.options_credit_spread.breeze_loader import ExpiryAwareLoader
from backtesting.options_credit_spread.expiry_calendar import ExpiryCalendar
from backtesting.options_credit_spread.supertrend_signals import (
    build_hourly_supertrend,
    extract_flip_events,
)
from backtesting.options_credit_spread.strategy_v2 import (
    MIN_DTE_TO_ENTER,
    run_backtest,
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def summarize(trades: list, skipped: list, label: str) -> dict:
    if not trades:
        print(f"\n=== {label}: no trades ===")
        return {}

    df = pd.DataFrame(trades).sort_values("exit_time").reset_index(drop=True)
    df["cum_pnl"] = df["pnl_rupees"].cumsum()
    df["drawdown"] = df["cum_pnl"] - df["cum_pnl"].cummax()

    total = df["pnl_rupees"].sum()
    win = (df["pnl_rupees"] > 0).mean() * 100
    gross_win = df.loc[df["pnl_rupees"] > 0, "pnl_rupees"].sum()
    gross_loss = -df.loc[df["pnl_rupees"] < 0, "pnl_rupees"].sum()
    pf = gross_win / gross_loss if gross_loss else float("inf")

    print(f"\n=== {label} ===")
    print(f"Trades: {len(df)} | Win: {win:.1f}% | PF: {pf:.2f} | "
          f"Net P&L: Rs {total:,.0f} | Avg: Rs {df['pnl_rupees'].mean():,.0f}")
    print(f"Max drawdown (cumulative P&L): Rs {df['drawdown'].min():,.0f}")

    if "rolled_to_next_expiry" in df:
        rolled = df[df["rolled_to_next_expiry"]]
        front = df[~df["rolled_to_next_expiry"]]
        print(f"\n  Front-week trades (v1 could take these): {len(front)}  "
              f"P&L Rs {front['pnl_rupees'].sum():,.0f}")
        print(f"  Rolled-expiry trades (NEW in v2)       : {len(rolled)}  "
              f"P&L Rs {rolled['pnl_rupees'].sum():,.0f}")
        if len(rolled):
            print(f"    their win rate: {(rolled['pnl_rupees'] > 0).mean()*100:.1f}%"
                  f"   avg: Rs {rolled['pnl_rupees'].mean():,.0f}")

    print("\n  P&L by exit reason:")
    print(df.groupby("exit_reason")["pnl_rupees"]
            .agg(["count", "sum", "mean"]).to_string())

    # Split the period in half. A result that only holds in one half is noise,
    # not an edge — this is the cheapest guard against fooling ourselves.
    mid = df["exit_time"].min() + (df["exit_time"].max() - df["exit_time"].min()) / 2
    first, second = df[df["exit_time"] <= mid], df[df["exit_time"] > mid]
    print("\n  Robustness — split-period check:")
    for name, part in (("first half ", first), ("second half", second)):
        if len(part):
            gw2 = part.loc[part["pnl_rupees"] > 0, "pnl_rupees"].sum()
            gl2 = -part.loc[part["pnl_rupees"] < 0, "pnl_rupees"].sum()
            pf2 = gw2 / gl2 if gl2 else float("inf")
            print(f"    {name}: {len(part):>3} trades  "
                  f"P&L Rs {part['pnl_rupees'].sum():>10,.0f}  "
                  f"win {(part['pnl_rupees'] > 0).mean()*100:>5.1f}%  PF {pf2:.2f}")

    if "approx_used" in df:
        n_approx = int(df["approx_used"].sum())
        print(f"\n  Trades using off-grid approximation: {n_approx} "
              f"(P&L Rs {df.loc[df['approx_used'], 'pnl_rupees'].sum():,.0f})")
    if skipped:
        print(f"\n  Signals still not tradeable: {len(skipped)}")
        print(pd.DataFrame(skipped)["reason"].value_counts().to_string())

    return {"label": label, "df": df, "trades": len(df), "pnl": total,
            "win": win, "pf": pf, "max_dd": df["drawdown"].min()}


def main() -> int:
    ap = argparse.ArgumentParser(description="Full-signal credit-spread backtest")
    ap.add_argument("--dry-run", action="store_true",
                    help="cost the run without spending API calls")
    ap.add_argument("--start", default=None, help="limit signals to on/after YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="limit signals to on/before YYYY-MM-DD")
    ap.add_argument("--roll-forward", action="store_true", default=True)
    ap.add_argument("--no-roll-forward", dest="roll_forward", action="store_false")
    ap.add_argument("--verbose", action="store_true", help="log each Breeze fetch")
    ap.add_argument("--no-stop-loss", dest="use_stop_loss", action="store_false",
                    default=True,
                    help="hold through paper losses; exit only on flip or expiry")
    ap.add_argument("--tag", default="", help="suffix for the results filename")
    args = ap.parse_args()

    print("Loading spot series and building 1H Supertrend...")
    loader = ExpiryAwareLoader(interval="1minute", dry_run=args.dry_run,
                               verbose=args.verbose)
    spot = loader.spot_1min_series()
    print(f"Spot: {len(spot):,} minutes, {spot.index.min()} -> {spot.index.max()}")

    hourly = build_hourly_supertrend(spot, period=10, multiplier=3.0)
    flips = extract_flip_events(hourly, period=10, multiplier=3.0)
    print(f"1H bars: {len(hourly):,} | Flip events: {len(flips)}")

    if args.start:
        flips = [f for f in flips if f[0] >= pd.Timestamp(args.start)]
    if args.end:
        flips = [f for f in flips if f[0] <= pd.Timestamp(args.end) + pd.Timedelta(days=1)]
    if args.start or args.end:
        print(f"Filtered to {len(flips)} flips in range")

    cal = ExpiryCalendar()
    dates = loader.available_dates()
    print(f"Trading days: {len(dates)} | Expiries in calendar: {len(cal.expiry_dates)}")

    # How many signals v1 would have thrown away, for reference.
    front_ok = sum(1 for t, _ in flips
                   if (d := cal.dte(t.strftime("%Y-%m-%d"))) is not None
                   and d >= MIN_DTE_TO_ENTER)
    print(f"\nSignals tradeable on the FRONT week (what v1 could use): "
          f"{front_ok}/{len(flips)} ({front_ok/max(1,len(flips))*100:.0f}%)")
    print(f"Signals v2 will route into a LATER expiry: {len(flips) - front_ok}")

    label = f"v2_roll_forward={args.roll_forward}"
    if not args.use_stop_loss:
        label += "_nostop"
    if args.tag:
        label += f"_{args.tag}"
    print(f"\nRunning backtest ({label})"
          f"{'  [STOP-LOSS DISABLED]' if not args.use_stop_loss else ''}"
          f"{' — DRY RUN' if args.dry_run else ''}...")
    trades, skipped = run_backtest(loader, cal, flips, dates,
                                   roll_forward=args.roll_forward,
                                   use_stop_loss=args.use_stop_loss)

    if args.dry_run:
        print("\nDRY RUN — no data was fetched, so P&L is meaningless. "
              "The number that matters is the fetch cost below.")
        loader.cost_report()
        return 0

    res = summarize(trades, skipped, label)
    loader.cost_report()

    if res:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / f"trades_{label}.csv"
        res["df"].to_csv(out, index=False)
        print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
