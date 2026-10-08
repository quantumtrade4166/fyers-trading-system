"""
Backtest the recommended setup on Breeze-sourced data (June 2026 onward).

ST(14, 2.5) + 2x premium stop, naked ATM selling, realistic costs.

This is a genuinely out-of-sample period: every parameter was chosen on data
ending 21 May 2026, so nothing here influenced any choice.

Note: pivots need the previous session, and Supertrend needs a warm-up, so the
first day (and roughly the first hour) of the range is unusable.

    python backtesting/options_pivot_intraday/run_breeze_period.py [start] [end]
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from backtesting.options_pivot_intraday.breeze_adapter import BreezeOptionsLoader
from backtesting.options_pivot_intraday.signals import (
    build_5min_bars, add_supertrend_7_3, build_daily_pivots,
)
from backtesting.options_pivot_intraday.strategy import run_backtest
from backtesting.options_pivot_intraday.run_costs import simulate, stats, SCENARIOS, REALISTIC

ST_PERIOD, ST_MULT = 14, 2.5
START = sys.argv[1] if len(sys.argv) > 1 else "2026-06-01"
END = sys.argv[2] if len(sys.argv) > 2 else "2026-09-11"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def main():
    slip_fn = {l: f for l, _, f in SCENARIOS}[REALISTIC]
    loader = BreezeOptionsLoader("NIFTY")
    spot = loader.spot_1min_series()
    print(f"Index spot: {len(spot):,} minutes | {spot.index.min()} to {spot.index.max()}")
    print(f"Option days available: {len(loader.available_dates())} "
          f"({loader.available_dates()[0]} to {loader.available_dates()[-1]})")

    bars = add_supertrend_7_3(build_5min_bars(spot), period=ST_PERIOD, multiplier=ST_MULT)
    pivots = build_daily_pivots(spot)
    dcol, scol = f"supertrend_dir_{ST_PERIOD}_{ST_MULT}", f"supertrend_{ST_PERIOD}_{ST_MULT}"
    print(f"Tradeable days (pivots available): {len(pivots)}\n")

    trades = run_backtest(loader, bars, pivots, dcol, scol,
                          stop_mult=2.0, date_from=START, date_to=END)
    if not trades:
        print("No trades generated.")
        return

    t = pd.DataFrame(trades).sort_values("exit_time").reset_index(drop=True)
    t["date"] = t["date"].astype(str)
    print(f"Trades: {len(t)} over {t['date'].nunique()} days "
          f"({t['date'].min()} to {t['date'].max()})\n")

    print("=" * 96)
    print(f"OUT-OF-SAMPLE: {START} to {END}  |  ST(14,2.5)+2x stop  |  realistic costs")
    print("=" * 96)
    for mode in ("fixed 10", "compound"):
        s = stats(simulate(t, mode == "compound", True, slip_fn))
        print(f"  {mode:9s} net=Rs{s['net_L']:6.2f}L  win={s['win%']}%  PF={s['PF']}  "
              f"maxDD={s['maxDD%']}%  avg/trade=Rs{s['cost_per_trade']:,} costs  "
              f"gross=Rs{s['gross_L']}L")

    sim = simulate(t, False, True, slip_fn)
    t["net"] = sim["net"].values

    print("\nBy exit reason (fixed 10 lots):")
    print(t.groupby("exit_reason")["net"].agg(["count", "sum", "mean"]).round(0).to_string())
    print("\nBy side:")
    print(t.groupby("side")["net"].agg(["count", "sum", "mean"]).round(0).to_string())

    t["month"] = pd.to_datetime(t["date"]).dt.strftime("%Y-%m")
    print("\nBy month (fixed 10 lots, realistic costs):")
    m = t.groupby("month")["net"].agg(trades=("size"), net=("sum"), avg=("mean")).round(0)
    m["win%"] = t.groupby("month")["net"].apply(lambda s: round((s > 0).mean() * 100, 1))
    m["cum"] = m["net"].cumsum()
    print(m.to_string())

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "trades_breeze_oos.csv"
    t.to_csv(out, index=False)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
