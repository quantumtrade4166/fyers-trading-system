import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
S019 Linear Regression Breakout — robustness investigation
=============================================================
S019 was the only strategy to retain a real out-of-sample edge. Before
trusting it we must distinguish two possibilities:

  (a) a genuine edge — the whole PARAMETER NEIGHBOURHOOD works, and the
      chosen cell is merely near the middle of a broad profitable region
  (b) noise — only the exact chosen cell works, and its neighbours don't

Test (b) is the one that kills most "discoveries". Everything below is
evaluated OUT-OF-SAMPLE (2003-2019), the data S019 was never fitted to.

Run: python backtesting/book_strategies/kaufman/run_s019_robustness.py
"""

import time
from pathlib import Path
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data import get_bars, get_bars_range
from backtesting.book_strategies.kaufman.harness import backtest
from backtesting.book_strategies.kaufman.strategies_channels import s019_linreg_breakout

RESULTS_DIR = Path(r"G:\Trading Brain\results")
CAPITAL = 20_000
RETAIL_COST = 0.35
OOS_START, OOS_END = "2003-05-05", "2019-08-23"

BASE_CFG = {
    "capital": CAPITAL, "risk_pct": 0.01, "max_position_pct": 0.50,
    "mode": "flat", "entry_on": "close", "default_stop_atr": 2.0,
    "cost_per_unit": RETAIL_COST,
}

CHOSEN = {"timeframe": "2h", "window": 50, "width_mult": 1.5}

WINDOWS = [20, 30, 40, 50, 60, 80, 100]
WIDTHS  = [1.0, 1.25, 1.5, 1.75, 2.0, 2.5]
TFS     = ["1h", "2h", "4h", "8h", "12h"]


def main():
    t0 = time.perf_counter()
    print("=" * 110)
    print("  S019 LINEAR REGRESSION BREAKOUT — ROBUSTNESS (all figures OUT-OF-SAMPLE 2003-2019)")
    print(f"  Chosen config: {CHOSEN}  |  ${CAPITAL:,}, 1% risk, cost ${RETAIL_COST:.2f}/oz")
    print("=" * 110)

    # ── 1. Parameter neighbourhood at the chosen timeframe ───────────────
    tf = CHOSEN["timeframe"]
    df = get_bars_range(OOS_START, OOS_END, tf, "oos")
    print(f"\n  [1] PARAMETER NEIGHBOURHOOD @ {tf} — Profit Factor (OOS)")
    print("      A genuine edge shows a broad profitable region, not one lucky cell.\n")

    pf_grid, net_grid = {}, {}
    for w in WINDOWS:
        pf_row, net_row = {}, {}
        for wm in WIDTHS:
            cfg = {**BASE_CFG, "window": w, "width_mult": wm}
            _, m = backtest(df.copy(), s019_linreg_breakout, cfg)
            pf_row[wm] = m["profit_factor"] if m["total_trades"] > 20 else np.nan
            net_row[wm] = m["net_pnl_usd"] if m["total_trades"] > 20 else np.nan
        pf_grid[w] = pf_row
        net_grid[w] = net_row

    pf_df = pd.DataFrame(pf_grid).T
    pf_df.index.name = "window"
    pf_df.columns.name = "width_mult"
    print(pf_df.round(3).to_string())

    vals = pf_df.to_numpy().flatten()
    vals = vals[~np.isnan(vals)]
    print(f"\n      cells tested          : {len(vals)}")
    print(f"      profitable (PF > 1.0) : {int((vals > 1.0).sum())} / {len(vals)}"
          f"  ({(vals > 1.0).mean()*100:.0f}%)")
    print(f"      PF > 1.10             : {int((vals > 1.10).sum())} / {len(vals)}")
    print(f"      median PF             : {np.median(vals):.3f}")
    print(f"      chosen cell PF        : {pf_df.loc[CHOSEN['window'], CHOSEN['width_mult']]:.3f}")

    print(f"\n  [1b] Same grid — NET PROFIT (USD, OOS)")
    net_df = pd.DataFrame(net_grid).T
    net_df.index.name = "window"; net_df.columns.name = "width_mult"
    print(net_df.round(0).to_string())

    # ── 2. Timeframe neighbourhood at the chosen parameters ──────────────
    print(f"\n  [2] TIMEFRAME NEIGHBOURHOOD @ window={CHOSEN['window']}, "
          f"width={CHOSEN['width_mult']} (OOS)")
    print(f"      {'TF':>5} {'trades':>8} {'PF':>8} {'Sharpe':>8} {'CAGR':>8} {'MaxDD':>9} {'net $':>11}")
    tf_rows = []
    for t in TFS:
        d = get_bars_range(OOS_START, OOS_END, t, "oos")
        cfg = {**BASE_CFG, "window": CHOSEN["window"], "width_mult": CHOSEN["width_mult"]}
        _, m = backtest(d.copy(), s019_linreg_breakout, cfg)
        tf_rows.append({"timeframe": t, **m})
        print(f"      {t:>5} {m['total_trades']:>8} {m['profit_factor']:>8.3f} "
              f"{m['sharpe']:>8.3f} {m['cagr_pct']:>7.2f}% {m['max_drawdown_pct']:>8.2f}% "
              f"{m['net_pnl_usd']:>11,.0f}")

    # ── 3. Stability across sub-periods ──────────────────────────────────
    print(f"\n  [3] SUB-PERIOD STABILITY @ chosen config — does it work in every regime?")
    periods = [
        ("2003-2007 bull",   "2003-05-05", "2007-12-31"),
        ("2008-2011 crisis+bull", "2008-01-01", "2011-08-31"),
        ("2011-2015 bear",   "2011-09-01", "2015-12-31"),
        ("2015-2019 range",  "2016-01-01", "2019-08-23"),
        ("2019-2026 IS",     None, None),
    ]
    print(f"      {'period':<24} {'trades':>8} {'PF':>8} {'Sharpe':>8} {'CAGR':>8} {'net $':>11}")
    for label, s, e in periods:
        cfg = {**BASE_CFG, "window": CHOSEN["window"], "width_mult": CHOSEN["width_mult"]}
        if s is None:
            d = get_bars(CHOSEN["timeframe"])
        else:
            d = get_bars_range(OOS_START, OOS_END, CHOSEN["timeframe"], "oos")
            d = d[(d.index >= s) & (d.index <= e)]
        if len(d) < 200:
            print(f"      {label:<24} (insufficient bars)")
            continue
        _, m = backtest(d.copy(), s019_linreg_breakout, cfg)
        print(f"      {label:<24} {m['total_trades']:>8} {m['profit_factor']:>8.3f} "
              f"{m['sharpe']:>8.3f} {m['cagr_pct']:>7.2f}% {m['net_pnl_usd']:>11,.0f}")

    pf_df.to_csv(RESULTS_DIR / f"s019_robustness_pf_grid_{date.today()}.csv")
    pd.DataFrame(tf_rows).to_csv(RESULTS_DIR / f"s019_robustness_tf_{date.today()}.csv", index=False)
    print(f"\n  Runtime: {time.perf_counter()-t0:.1f}s\n")


if __name__ == "__main__":
    main()
