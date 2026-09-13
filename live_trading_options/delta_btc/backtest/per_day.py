"""
backtest/per_day.py — the same parameters, one day at a time.

A sweep total answers "which setting made the most money overall". It cannot
answer "does this setting survive a bad day", and for a short-premium strategy
that is the only question worth asking. A stop multiple that wins three quiet days
by $100 each and gives back $500 on one trending day has a positive-looking
average and a ruinous distribution.

So each candidate is run per day, side by side, with the day's BTC range printed
next to it — a day that moved 3% is a different test from a day that moved 0.5%,
and the stop that looks best on the quiet one is exactly the one to distrust.

Run:
    .venv/Scripts/python.exe live_trading_options/delta_btc/backtest/per_day.py
    ... --profile full_cycle
    ... --target 50 --mults 1.2,1.5,2.0
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import argparse

from backtest.replay import load_days, available_days, coverage
from backtest.run_backtest import run_one, summarise, PARAMS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="ist_day")
    ap.add_argument("--target", type=float, default=50.0)
    ap.add_argument("--mults", default="1.2,1.5,2.0")
    ap.add_argument("--days", help="comma-separated YYYY-MM-DD")
    ap.add_argument("--min-coverage", type=float, default=40.0)
    args = ap.parse_args()

    mults = [float(x) for x in args.mults.split(",")]
    days = args.days.split(",") if args.days else available_days()
    base = {k: v for k, v in PARAMS.items() if k != "sessions"}

    print(f"\n  {args.profile}  ·  target {args.target:g}  ·  SL multiples {mults}\n")
    head = f"  {'day':<12}{'cover':>7}{'BTC range':>12}"
    for m in mults:
        head += f"{('x' + format(m, 'g')):>16}"
    print(head)
    print("  " + "─" * (len(head) - 2))

    totals = {m: 0.0 for m in mults}
    stops = {m: 0 for m in mults}
    # None, not 0.0: starting the minimum at zero reports a worst day of $0.00 when
    # every day was profitable, which reads as "no losing day" for the wrong reason.
    worst = {m: None for m in mults}
    for day in days:
        df = load_days([day])
        if df.empty:
            continue
        cov = coverage(df).get(day, {})
        if cov.get("pct_of_day", 0) < args.min_coverage:
            continue
        spot = df["spot"].dropna()
        rng = f"{(spot.max() - spot.min()) / spot.mean() * 100:.2f}%" if len(spot) else "?"
        row = f"  {day:<12}{cov['pct_of_day']:>6.0f}%{rng:>12}"
        for m in mults:
            cfg = dict(PARAMS["sessions"][args.profile])
            cfg["target_premium"] = args.target
            cfg["sl_premium"] = args.target * 2
            prm = dict(base)
            prm["sl_combined_multiple"] = m
            s = summarise(run_one(args.profile, cfg, prm, df))
            totals[m] += s["total"]
            stops[m] += s.get("stops", 0)
            worst[m] = s["total"] if worst[m] is None else min(worst[m], s["total"])
            row += f"{s['total']:>+11.2f} /{s.get('stops', 0):<3}"
        print(row)

    print("  " + "─" * (len(head) - 2))
    row = f"  {'TOTAL':<31}"
    for m in mults:
        row += f"{totals[m]:>+11.2f} /{stops[m]:<3}"
    print(row)
    row = f"  {'worst single day':<31}"
    for m in mults:
        row += (f"{worst[m]:>+11.2f}    " if worst[m] is not None else f"{'—':>11}    ")
    print(row)
    print("\n  cell = P&L / stop-outs.  Read the WORST DAY row before the total.\n")


if __name__ == "__main__":
    main()
