import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Mega hunt — every implemented strategy x every instrument x every timeframe
==============================================================================
TARGET: CAGR > 30% with MaxDD < 10%  ->  CAGR/DD ratio >= 3.0

Current best (gold + S&P + Nasdaq, 12 members): ratio 1.07.
This widens the search to 78 strategies across 8 instruments and 6 timeframes,
each validated out-of-sample, then feeds the survivors to the portfolio builder.

Timeframes 5min/15min/30min are excluded: across 237 tested combinations they
produced ZERO profitable strategies out-of-sample, because trading costs at
that frequency exceed the account (the average 5-minute strategy paid $82,625
of spread on a $20,000 account over 8 years).

Split: OOS = start of data → 2019-08-23 (never fitted)
       IS  = 2019-08-23 → present

Run: python backtesting/book_strategies/kaufman/run_mega_hunt.py
"""

import time
import warnings
from pathlib import Path
from datetime import date

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data_multi import (
    get_bars, available, COSTS, LABELS)
from backtesting.book_strategies.kaufman.harness import backtest
from backtesting.book_strategies.kaufman.strategies_adaptive import REGISTRY as R_A
from backtesting.book_strategies.kaufman.strategies_channels import (
    REGISTRY as R_C, STRATEGY_CFG as CFG_C)
from backtesting.book_strategies.kaufman.strategies_bulk import (
    REGISTRY as R_B, STRATEGY_CFG as CFG_B)
from backtesting.book_strategies.kaufman.strategies_bulk2 import (
    REGISTRY as R_B2, STRATEGY_CFG as CFG_B2)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CAPITAL = 20_000
SPLIT = "2019-08-23"
TIMEFRAMES = ["1h", "2h", "4h", "8h", "12h", "1D"]
MIN_TRADES = 40

REGISTRY = {**R_B, **R_B2, **R_A, **R_C}
PER_STRATEGY = {**CFG_B, **CFG_B2, **CFG_C, "S092_AdaptiveBrkout": {"trailing": True}}


def ratio(m):
    dd = abs(m.get("max_drawdown_pct", 0))
    return m.get("cagr_pct", 0) / dd if dd > 0.01 else 0.0


def main():
    t0 = time.perf_counter()
    insts = [i for i in available() if i in COSTS]
    print("=" * 122)
    print("  MEGA HUNT — target CAGR > 30% with MaxDD < 10%  (ratio >= 3.0)")
    print(f"  {len(REGISTRY)} strategies x {len(insts)} instruments x {len(TIMEFRAMES)} timeframes"
          f" = {len(REGISTRY)*len(insts)*len(TIMEFRAMES):,} runs, each IS + OOS")
    print(f"  ${CAPITAL:,}, 1% risk/trade | split {SPLIT}")
    print("=" * 122)

    rows = []
    for inst in insts:
        for tf in TIMEFRAMES:
            try:
                d_is  = get_bars(inst, tf, start=SPLIT)
                d_oos = get_bars(inst, tf, end=SPLIT)
            except Exception as e:
                print(f"  [{inst} {tf}] skip: {e}")
                continue
            if len(d_is) < 300 or len(d_oos) < 300:
                continue
            t1 = time.perf_counter(); n_ok = 0
            for name, fn in REGISTRY.items():
                cfg = {"capital": CAPITAL, "risk_pct": 0.01, "max_position_pct": 0.50,
                       "mode": "flat", "entry_on": "close", "default_stop_atr": 2.0,
                       "cost_per_unit": COSTS[inst], **PER_STRATEGY.get(name, {})}
                try:
                    _, mi = backtest(d_is.copy(), fn, cfg)
                    if mi.get("total_trades", 0) < MIN_TRADES:
                        continue
                    _, mo = backtest(d_oos.copy(), fn, cfg)
                    if mo.get("total_trades", 0) < MIN_TRADES:
                        continue
                except Exception:
                    continue
                rows.append({"instrument": inst, "label": LABELS[inst], "strategy": name,
                             "timeframe": tf, "is_ratio": ratio(mi), "oos_ratio": ratio(mo),
                             **{f"is_{k}": v for k, v in mi.items()},
                             **{f"oos_{k}": v for k, v in mo.items()}})
                n_ok += 1
            print(f"  [{LABELS[inst]:<12} {tf:>4}] {len(d_is):>7,} IS / {len(d_oos):>7,} OOS bars"
                  f" — {n_ok:>2} passed ({time.perf_counter()-t1:.0f}s)")

    res = pd.DataFrame(rows)
    out = RESULTS_DIR / f"kaufman_mega_hunt_{date.today()}.csv"
    res.to_csv(out, index=False)
    print(f"\n  {len(res):,} combinations with enough trades in both periods")

    surv = res[(res["oos_net_pnl_usd"] > 0) & (res["oos_profit_factor"] > 1.0)]
    print(f"  Out-of-sample survivors: {len(surv):,} ({len(surv)/max(len(res),1)*100:.1f}%)")

    print("\n" + "=" * 122)
    print("  SURVIVORS BY INSTRUMENT")
    print("=" * 122)
    g = res.groupby("label").agg(
        tested=("strategy", "count"),
        survived=("oos_net_pnl_usd", lambda s: int((s > 0).sum())),
        best_oos_sharpe=("oos_sharpe", "max"),
        best_oos_ratio=("oos_ratio", "max"),
        median_oos_sharpe=("oos_sharpe", "median"))
    g["survival_%"] = (g.survived / g.tested * 100).round(1)
    print(g.sort_values("best_oos_ratio", ascending=False).round(3).to_string())

    print("\n" + "=" * 122)
    print("  TOP 30 BY OUT-OF-SAMPLE RATIO (CAGR/DD)")
    print("=" * 122)
    cols = ["label", "strategy", "timeframe", "oos_total_trades", "oos_profit_factor",
            "oos_sharpe", "oos_cagr_pct", "oos_max_drawdown_pct", "oos_ratio"]
    print(surv.sort_values("oos_ratio", ascending=False).head(30)[cols].to_string(index=False))

    print("\n  Top 15 by out-of-sample Sharpe:")
    print(surv.sort_values("oos_sharpe", ascending=False).head(15)[cols].to_string(index=False))

    hit = surv[(surv.oos_cagr_pct >= 30) & (surv.oos_max_drawdown_pct >= -10)]
    print(f"\n  Single strategies already at CAGR>=30% and DD<=10% OOS: {len(hit)}")
    if len(hit):
        print(hit[cols].to_string(index=False))

    print(f"\n  CSV: {out}")
    print(f"  Runtime: {time.perf_counter()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
