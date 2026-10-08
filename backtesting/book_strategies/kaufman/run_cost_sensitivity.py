import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Cost sensitivity — the three Batch C survivors under realistic XAUUSD friction
================================================================================
Charges a round-trip cost per ounce (spread + slippage + commission) and
sweeps it, so we can see not just "does it survive typical costs" but
**at what cost level does each strategy break even** — the honest measure
of how much real edge there is.

Run: python backtesting/book_strategies/kaufman/run_cost_sensitivity.py
"""

import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data import get_bars, TEST_YEARS, START_DATE, END_DATE
from backtesting.book_strategies.kaufman.harness import backtest
from backtesting.book_strategies.kaufman.strategies_adaptive import (
    s088_kama, s089_vidya, s092_adaptive_breakout,
)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CAPITAL = 20_000

BASE = {
    "capital": CAPITAL, "risk_pct": 0.01, "max_position_pct": 0.50,
    "mode": "flat", "entry_on": "close", "default_stop_atr": 2.0,
}

# Best config found in the Batch C sweep, per strategy
SURVIVORS = {
    "S088_KAMA":   (s088_kama,   "4h", {"er_period": 30, "slow": 30, "stop_atr_mult": 3.0}),
    "S089_VIDYA":  (s089_vidya,  "4h", {"short_period": 9, "cmo_period": 20, "stop_atr_mult": 3.0}),
    "S092_Brkout": (s092_adaptive_breakout, "4h",
                    {"n_bars": 5, "stop_atr_mult": 3.0, "trailing": True}),
}

# Round-trip cost per ounce, USD
COST_LEVELS = [0.00, 0.10, 0.20, 0.35, 0.50, 0.70, 1.00]

COST_LABELS = {
    0.00: "frictionless (fantasy)",
    0.10: "institutional/very tight",
    0.20: "tight ECN + commission",
    0.35: "typical retail",
    0.50: "retail + real slippage",
    0.70: "wide / fast market",
    1.00: "poor conditions",
}


def main():
    t0 = time.perf_counter()
    print("=" * 118)
    print(f"  COST SENSITIVITY — Kaufman Batch C survivors")
    print(f"  XAUUSD 4h  |  {START_DATE} -> {END_DATE} ({TEST_YEARS}y)  |  ${CAPITAL:,} start, 1% risk/trade")
    print(f"  Cost = round-trip USD per ounce (spread + slippage + commission), charged per completed trade")
    print("=" * 118)

    df_cache = {}
    rows = []

    for name, (fn, tf, params) in SURVIVORS.items():
        if tf not in df_cache:
            df_cache[tf] = get_bars(tf)
        df = df_cache[tf]

        print(f"\n  {name}  ({tf}, {params})")
        print(f"  {'cost/oz':>9}  {'label':<26} {'trades':>7} {'win%':>6} {'PF':>6} "
              f"{'Sharpe':>7} {'CAGR':>8} {'MaxDD':>8} {'gross $':>11} {'costs $':>10} {'net $':>11} {'final $':>11}")
        print("  " + "-" * 114)

        for cost in COST_LEVELS:
            cfg = {**BASE, **params, "cost_per_unit": cost}
            _, m = backtest(df.copy(), fn, cfg)
            rows.append({"strategy": name, "timeframe": tf, "cost_per_oz": cost, **m})

            print(f"  {cost:>9.2f}  {COST_LABELS[cost]:<26} "
                  f"{m['total_trades']:>7} {m['win_rate_pct']:>6.1f} {m['profit_factor']:>6.3f} "
                  f"{m['sharpe']:>7.3f} {m['cagr_pct']:>7.2f}% {m['max_drawdown_pct']:>7.2f}% "
                  f"{m['gross_pnl_usd']:>11,.0f} {m['total_cost_usd']:>10,.0f} "
                  f"{m['net_pnl_usd']:>11,.0f} {m['final_equity_usd']:>11,.0f}")

    res = pd.DataFrame(rows)
    out = RESULTS_DIR / f"kaufman_batchC_cost_sensitivity_{END_DATE}.csv"
    res.to_csv(out, index=False)

    # Breakeven cost per strategy — where net PnL crosses zero
    print("\n" + "=" * 118)
    print("  BREAKEVEN COST — the cost level at which each strategy stops making money")
    print("=" * 118)
    for name in SURVIVORS:
        sub = res[res["strategy"] == name].sort_values("cost_per_oz")
        pos = sub[sub["net_pnl_usd"] > 0]
        neg = sub[sub["net_pnl_usd"] <= 0]
        if neg.empty:
            print(f"  {name:<14} still profitable at $1.00/oz — robust to cost")
        elif pos.empty:
            print(f"  {name:<14} unprofitable even frictionless")
        else:
            last_ok = pos["cost_per_oz"].max()
            first_bad = neg["cost_per_oz"].min()
            print(f"  {name:<14} breaks even between ${last_ok:.2f} and ${first_bad:.2f} per ounce")

    print(f"\n  CSV: {out}")
    print(f"  Runtime: {time.perf_counter()-t0:.1f}s\n")


if __name__ == "__main__":
    main()
