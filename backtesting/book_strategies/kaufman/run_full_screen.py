import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Full screen — every strategy × every viable timeframe, then OOS validation
=============================================================================
GOAL: find strategies with CAGR > 10% and MaxDD < 10%.

Because CAGR and MaxDD both scale ~linearly with risk-per-trade, the target is
really a RATIO requirement: CAGR/MaxDD > 1.0. S019 (our only validated
strategy) sits at 1.59/7.18 = 0.22. Sizing up cannot fix that — only better
strategies, or a portfolio of several uncorrelated ones, can.

So this screen ranks on CAGR/DD ratio and OOS Sharpe, not on raw return.

Stage 1: in-sample screen (2019-2026, 7y) across 1h..1D
Stage 2: out-of-sample validation (2003-2019, 16y) of everything that passed
Stage 3: writes a CSV the portfolio search consumes

15min/30min are excluded — the timeframe matrix already showed 0/15 and 4/15
strategies profitable there.

Run: python backtesting/book_strategies/kaufman/run_full_screen.py
"""

import time
import warnings
from pathlib import Path
from datetime import date

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data import get_bars, get_bars_range
from backtesting.book_strategies.kaufman.harness import backtest
from backtesting.book_strategies.kaufman.strategies_adaptive import REGISTRY as R_ADAPT
from backtesting.book_strategies.kaufman.strategies_channels import (
    REGISTRY as R_CHAN, STRATEGY_CFG as CFG_CHAN)
from backtesting.book_strategies.kaufman.strategies_bulk import (
    REGISTRY as R_BULK, STRATEGY_CFG as CFG_BULK)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CAPITAL = 20_000
RETAIL_COST = 0.35
OOS_START, OOS_END = "2003-05-05", "2019-08-23"

BASE_CFG = {
    "capital": CAPITAL, "risk_pct": 0.01, "max_position_pct": 0.50,
    "mode": "flat", "entry_on": "close", "default_stop_atr": 2.0,
    "cost_per_unit": RETAIL_COST,
}

TIMEFRAMES = ["1h", "2h", "4h", "8h", "12h", "1D"]

REGISTRY = {**R_BULK, **R_ADAPT, **R_CHAN}
PER_STRATEGY = {**CFG_BULK, **CFG_CHAN, "S092_AdaptiveBrkout": {"trailing": True}}

# Stage-1 gate: worth OOS-testing at all
MIN_TRADES = 40
MIN_IS_SHARPE = 0.40


def _ratio(m: dict) -> float:
    dd = abs(m.get("max_drawdown_pct", 0))
    return m.get("cagr_pct", 0) / dd if dd > 0.01 else 0.0


def main():
    t0 = time.perf_counter()
    print("=" * 116)
    print("  FULL STRATEGY SCREEN — hunting CAGR > 10% with MaxDD < 10%")
    print(f"  {len(REGISTRY)} strategies x {len(TIMEFRAMES)} timeframes  |  "
          f"${CAPITAL:,}, 1% risk, cost ${RETAIL_COST:.2f}/oz")
    print("  Target restated: CAGR/DD ratio > 1.0 (both scale with risk, so the RATIO is what matters)")
    print("=" * 116)

    # ── Stage 1: in-sample screen ────────────────────────────────────────
    rows = []
    for tf in TIMEFRAMES:
        df = get_bars(tf)
        t1 = time.perf_counter()
        n_ok = 0
        for name, fn in REGISTRY.items():
            cfg = {**BASE_CFG, **PER_STRATEGY.get(name, {})}
            try:
                _, m = backtest(df.copy(), fn, cfg)
            except Exception:
                continue
            if m.get("total_trades", 0) < MIN_TRADES:
                continue
            rows.append({"strategy": name, "timeframe": tf,
                         "is_ratio": _ratio(m),
                         **{f"is_{k}": v for k, v in m.items()}})
            n_ok += 1
        print(f"  [IS {tf:>4}] {len(df):>7,} bars — {n_ok} strategies "
              f"({time.perf_counter()-t1:.0f}s)")

    scr = pd.DataFrame(rows)
    print(f"\n  Stage 1 complete: {len(scr)} strategy/timeframe combinations")

    passed = scr[(scr["is_sharpe"] >= MIN_IS_SHARPE)].copy()
    print(f"  Passing stage-1 gate (IS Sharpe >= {MIN_IS_SHARPE}): {len(passed)}")

    print(f"\n  Best in-sample CAGR/DD ratios:")
    top = scr.sort_values("is_ratio", ascending=False).head(10)
    for _, r in top.iterrows():
        print(f"    {r['strategy']:<20}{r['timeframe']:>5}  ratio {r['is_ratio']:>5.2f}  "
              f"CAGR {r['is_cagr_pct']:>6.2f}%  DD {r['is_max_drawdown_pct']:>7.2f}%  "
              f"Sharpe {r['is_sharpe']:>5.2f}")

    # ── Stage 2: out-of-sample validation ────────────────────────────────
    print(f"\n  Stage 2: out-of-sample validation of {len(passed)} candidates "
          f"(2003-2019, never fitted)...")
    oos_rows = []
    for tf in sorted(passed["timeframe"].unique()):
        d = get_bars_range(OOS_START, OOS_END, tf, "oos")
        sub = passed[passed["timeframe"] == tf]
        for _, r in sub.iterrows():
            name = r["strategy"]
            cfg = {**BASE_CFG, **PER_STRATEGY.get(name, {})}
            try:
                _, m = backtest(d.copy(), REGISTRY[name], cfg)
            except Exception:
                continue
            oos_rows.append({"strategy": name, "timeframe": tf,
                             "oos_ratio": _ratio(m),
                             **{f"oos_{k}": v for k, v in m.items()}})
        print(f"    [OOS {tf:>4}] {len(sub)} tested")

    oos = pd.DataFrame(oos_rows)
    merged = passed.merge(oos, on=["strategy", "timeframe"], how="inner")
    out = RESULTS_DIR / f"kaufman_full_screen_{date.today()}.csv"
    merged.to_csv(out, index=False)

    # ── Stage 3: report ──────────────────────────────────────────────────
    surv = merged[(merged["oos_net_pnl_usd"] > 0) &
                  (merged["oos_profit_factor"] > 1.0)].copy()
    surv = surv.sort_values("oos_sharpe", ascending=False)

    print("\n" + "=" * 116)
    print(f"  OUT-OF-SAMPLE SURVIVORS — {len(surv)} of {len(merged)} candidates")
    print("=" * 116)
    print(f"  {'strategy':<20}{'TF':>5}{'trades':>8}{'OOS PF':>8}{'OOS Sh':>8}"
          f"{'OOS CAGR':>10}{'OOS DD':>9}{'ratio':>7}{'IS Sh':>7}")
    print("  " + "-" * 82)
    for _, r in surv.head(30).iterrows():
        print(f"  {r['strategy']:<20}{r['timeframe']:>5}{r['oos_total_trades']:>8}"
              f"{r['oos_profit_factor']:>8.3f}{r['oos_sharpe']:>8.3f}"
              f"{r['oos_cagr_pct']:>9.2f}%{r['oos_max_drawdown_pct']:>8.2f}%"
              f"{r['oos_ratio']:>7.2f}{r['is_sharpe']:>7.2f}")

    peers = surv[surv["oos_sharpe"] >= 0.80]
    print(f"\n  Peers to S019 (OOS Sharpe >= 0.80): {len(peers)}")
    for _, r in peers.iterrows():
        print(f"    {r['strategy']:<20}{r['timeframe']:>5}  OOS Sharpe {r['oos_sharpe']:.3f}  "
              f"CAGR {r['oos_cagr_pct']:.2f}%  DD {r['oos_max_drawdown_pct']:.2f}%")

    print(f"\n  CSV: {out}")
    print(f"  Runtime: {time.perf_counter()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
