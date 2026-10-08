import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Index hunt — every strategy x every timeframe on S&P 500 and Nasdaq 100
==========================================================================
TARGET: CAGR > 10% (ideally 25%+) with MaxDD < 10%, at 0.5-1% risk.

Restated honestly: risk sizing scales CAGR and MaxDD together, so the target
is a RATIO requirement that risk % cannot change:
    CAGR 10% / DD 10%  ->  ratio 1.0
    CAGR 25% / DD 10%  ->  ratio 2.5
Gold's best portfolio managed 0.47. This tests whether index CFDs — which
carry strong upward drift that suits trend systems — do materially better.

Timeframes: 5min → 1D (user asked to start at 5min).
Split: OOS 2011-09→2019-08 (older, never fitted) | IS 2019-08→2026-08.
Note the index CFD series only begins 2011-09, so OOS is ~8y not gold's 16y.

Run: python backtesting/book_strategies/kaufman/run_index_hunt.py
"""

import time
import warnings
from pathlib import Path
from datetime import date

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data_multi import get_bars, COSTS, LABELS
from backtesting.book_strategies.kaufman.harness import backtest
from backtesting.book_strategies.kaufman.strategies_adaptive import REGISTRY as R_ADAPT
from backtesting.book_strategies.kaufman.strategies_channels import (
    REGISTRY as R_CHAN, STRATEGY_CFG as CFG_CHAN)
from backtesting.book_strategies.kaufman.strategies_bulk import (
    REGISTRY as R_BULK, STRATEGY_CFG as CFG_BULK)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CAPITAL   = 20_000
RISK_PCT  = 0.01                 # screen at 1%; final numbers rescaled to fit DD cap
INSTRUMENTS = ["usa500idxusd", "usatechidxusd"]
TIMEFRAMES  = ["5min", "15min", "30min", "1h", "2h", "4h", "8h", "12h", "1D"]

SPLIT   = "2019-08-23"
OOS_BEG = "2011-09-01"

REGISTRY = {**R_BULK, **R_ADAPT, **R_CHAN}
PER_STRATEGY = {**CFG_BULK, **CFG_CHAN, "S092_AdaptiveBrkout": {"trailing": True}}

MIN_TRADES = 30


def cfg_for(inst, name, extra=None):
    return {"capital": CAPITAL, "risk_pct": RISK_PCT, "max_position_pct": 0.50,
            "mode": "flat", "entry_on": "close", "default_stop_atr": 2.0,
            "cost_per_unit": COSTS[inst],
            **PER_STRATEGY.get(name, {}), **(extra or {})}


def ratio(m):
    dd = abs(m.get("max_drawdown_pct", 0))
    return m.get("cagr_pct", 0) / dd if dd > 0.01 else 0.0


def main():
    t0 = time.perf_counter()
    print("=" * 120)
    print("  INDEX HUNT — S&P 500 + Nasdaq 100, all strategies, 5min → 1D")
    print(f"  ${CAPITAL:,}, {RISK_PCT*100:.1f}% risk/trade | IS {SPLIT}→2026-08 | OOS {OOS_BEG}→{SPLIT}")
    print(f"  Costs: S&P {COSTS['usa500idxusd']} pts, Nasdaq {COSTS['usatechidxusd']} pts (round trip)")
    print(f"  Target ratio: 1.0 for 10%/10%, 2.5 for 25%/10%")
    print("=" * 120)

    rows = []
    for inst in INSTRUMENTS:
        for tf in TIMEFRAMES:
            try:
                df_is  = get_bars(inst, tf, start=SPLIT)
                df_oos = get_bars(inst, tf, start=OOS_BEG, end=SPLIT)
            except Exception as e:
                print(f"  [{inst} {tf}] load failed: {e}")
                continue
            t1 = time.perf_counter()
            n_ok = 0
            for name, fn in REGISTRY.items():
                c = cfg_for(inst, name)
                try:
                    _, mi = backtest(df_is.copy(), fn, c)
                    if mi.get("total_trades", 0) < MIN_TRADES:
                        continue
                    _, mo = backtest(df_oos.copy(), fn, c)
                except Exception:
                    continue
                rows.append({
                    "instrument": inst, "label": LABELS[inst], "strategy": name,
                    "timeframe": tf, "is_ratio": ratio(mi), "oos_ratio": ratio(mo),
                    **{f"is_{k}": v for k, v in mi.items()},
                    **{f"oos_{k}": v for k, v in mo.items()},
                })
                n_ok += 1
            print(f"  [{LABELS[inst]:<11} {tf:>5}] {len(df_is):>8,} IS bars — "
                  f"{n_ok:>2} strategies ({time.perf_counter()-t1:.0f}s)")

    res = pd.DataFrame(rows)
    out = RESULTS_DIR / f"kaufman_index_hunt_{date.today()}.csv"
    res.to_csv(out, index=False)
    print(f"\n  {len(res)} instrument/strategy/timeframe combinations tested")

    # ── Timeframe viability ──────────────────────────────────────────────
    print("\n" + "=" * 120)
    print("  TIMEFRAME VIABILITY (out-of-sample)")
    print("=" * 120)
    agg = res.groupby(["label", "timeframe"]).agg(
        n=("strategy", "count"),
        oos_profitable=("oos_net_pnl_usd", lambda s: int((s > 0).sum())),
        median_oos_ratio=("oos_ratio", "median"),
        best_oos_ratio=("oos_ratio", "max"),
        best_oos_sharpe=("oos_sharpe", "max"),
    ).reset_index()
    agg["timeframe"] = pd.Categorical(agg["timeframe"], TIMEFRAMES, ordered=True)
    print(agg.sort_values(["label", "timeframe"]).to_string(index=False))

    # ── Survivors ────────────────────────────────────────────────────────
    surv = res[(res["oos_net_pnl_usd"] > 0) & (res["oos_profit_factor"] > 1.0)]
    print("\n" + "=" * 120)
    print(f"  OUT-OF-SAMPLE SURVIVORS: {len(surv)} of {len(res)}")
    print("=" * 120)
    cols = ["label", "strategy", "timeframe", "oos_total_trades", "oos_profit_factor",
            "oos_sharpe", "oos_cagr_pct", "oos_max_drawdown_pct", "oos_ratio", "is_ratio"]
    top = surv.sort_values("oos_ratio", ascending=False).head(25)
    print(top[cols].to_string(index=False))

    print("\n  Best by OOS Sharpe:")
    print(surv.sort_values("oos_sharpe", ascending=False).head(12)[cols].to_string(index=False))

    hits = surv[(surv["oos_cagr_pct"] >= 10) & (surv["oos_max_drawdown_pct"] >= -10)]
    print(f"\n  Single strategies already meeting CAGR>=10% AND DD<=10% out-of-sample: {len(hits)}")
    if len(hits):
        print(hits[cols].to_string(index=False))

    print(f"\n  CSV: {out}")
    print(f"  Runtime: {time.perf_counter()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
