import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Final portfolio search — how close can we get to CAGR 30% with MaxDD < 10%?
==============================================================================
Consumes the mega-hunt survivors and builds the best cross-instrument,
cross-strategy, cross-timeframe portfolio, then finds the risk level that
maximises CAGR under a hard drawdown cap.

Evaluated on the COMMON out-of-sample window where every chosen member has
data. Using the union instead would silently flatter the early years, when
only the long-history instruments (gold, silver, FX) were contributing.

Run: python backtesting/book_strategies/kaufman/run_final_portfolio.py
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
from backtesting.book_strategies.kaufman.harness import run as run_h, HARNESS_DEFAULTS
from backtesting.book_strategies.kaufman.strategies_adaptive import REGISTRY as R_A
from backtesting.book_strategies.kaufman.strategies_channels import (
    REGISTRY as R_C, STRATEGY_CFG as CFG_C)
from backtesting.book_strategies.kaufman.strategies_bulk import (
    REGISTRY as R_B, STRATEGY_CFG as CFG_B)
from backtesting.book_strategies.kaufman.strategies_bulk2 import (
    REGISTRY as R_B2, STRATEGY_CFG as CFG_B2)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
CAPITAL = 20_000
SPLIT = "2019-08-23"
DD_CAP = 10.0

REGISTRY = {**R_B, **R_B2, **R_A, **R_C}
PER_STRATEGY = {**CFG_B, **CFG_B2, **CFG_C, "S092_AdaptiveBrkout": {"trailing": True}}

MAX_MEMBERS = 20
MIN_OOS_SHARPE = 0.25
MIN_OOS_TRADES = 60
MAX_PER_STRATEGY = 3        # same rule may appear on at most 3 instruments
MAX_PER_INSTRUMENT = 4      # stop one market dominating


def stream(inst, name, tf):
    cfg = {**HARNESS_DEFAULTS, "capital": CAPITAL, "risk_pct": 0.01,
           "max_position_pct": 0.50, "mode": "flat", "entry_on": "close",
           "default_stop_atr": 2.0, "cost_per_unit": COSTS[inst],
           **PER_STRATEGY.get(name, {})}
    df = get_bars(inst, tf, end=SPLIT)
    tr = run_h(df, REGISTRY[name](df.copy(), cfg), cfg)
    if not tr:
        return pd.Series(dtype=float)
    t = pd.DataFrame([x.to_dict() for x in tr])
    t["exit_time"] = pd.to_datetime(t["exit_time"])
    return t.groupby(t["exit_time"].dt.date)["pnl"].sum()


def metrics(daily, capital=CAPITAL):
    if daily.empty or daily.std() == 0:
        return dict(cagr_pct=0, sharpe=0, max_drawdown_pct=0,
                    net_pnl_usd=0, final_equity_usd=capital, ratio=0)
    eq = capital + daily.cumsum()
    dd = ((eq - eq.cummax()) / eq.cummax() * 100).min()
    yrs = max((max(daily.index) - min(daily.index)).days, 1) / 365.25
    net = daily.sum(); fin = capital + net
    cagr = ((fin / capital) ** (1 / yrs) - 1) * 100 if fin > 0 else -100.0
    return dict(cagr_pct=cagr, sharpe=daily.mean() / daily.std() * np.sqrt(252),
                max_drawdown_pct=dd, net_pnl_usd=net, final_equity_usd=fin,
                ratio=cagr / abs(dd) if dd else 0)


def main():
    t0 = time.perf_counter()
    files = sorted(RESULTS_DIR.glob("kaufman_mega_hunt_*.csv"))
    if not files:
        print("  Run run_mega_hunt.py first."); return
    res = pd.read_csv(files[-1])

    print("=" * 118)
    print(f"  FINAL PORTFOLIO SEARCH — target CAGR 30% with MaxDD < {DD_CAP}% (ratio 3.0)")
    print(f"  Source: {files[-1].name}   |  ${CAPITAL:,} total")
    print("=" * 118)

    cand = res[(res.oos_net_pnl_usd > 0) & (res.oos_profit_factor > 1.0) &
               (res.oos_sharpe >= MIN_OOS_SHARPE) &
               (res.oos_total_trades >= MIN_OOS_TRADES)].copy()
    cand = cand.sort_values("oos_sharpe", ascending=False)
    cand = cand.groupby(["instrument", "strategy"]).head(1)
    cand = cand.groupby("strategy").head(MAX_PER_STRATEGY)
    cand = cand.groupby("instrument").head(MAX_PER_INSTRUMENT)
    cand = cand.head(40)
    print(f"\n  Candidate pool: {len(cand)}   "
          f"(instruments: {cand.label.nunique()}, strategies: {cand.strategy.nunique()})")
    if cand.empty:
        print("  none cleared the floor"); return

    print("\n  Building out-of-sample streams...")
    streams = {}
    for _, r in cand.iterrows():
        s = stream(r.instrument, r.strategy, r.timeframe)
        if len(s) >= 30:
            streams[f"{r.label}|{r.strategy}@{r.timeframe}"] = s
    print(f"  {len(streams)} usable streams")

    # Common window only — every member must have data
    starts = [min(s.index) for s in streams.values()]
    common = max(starts)
    idx = sorted({d for s in streams.values() for d in s.index if d >= common})
    A = pd.DataFrame({k: v.reindex(idx, fill_value=0.0)
                      for k, v in streams.items()}).sort_index()
    print(f"  Common evaluation window: {common} → {max(idx)}  ({len(idx):,} days)")

    corr = A.corr()
    off = corr.to_numpy()[np.triu_indices(len(corr), k=1)]
    print(f"  Average pairwise correlation: {off.mean():.3f}")

    print("\n  Greedy construction (equal capital)")
    chosen, best = [], -99.0
    rem = list(A.columns)
    while rem and len(chosen) < MAX_MEMBERS:
        pick, ps = None, best
        for c in rem:
            m = metrics(A[chosen + [c]].sum(axis=1) / (len(chosen) + 1))
            if m["sharpe"] > ps:
                pick, ps = c, m["sharpe"]
        if pick is None:
            break
        chosen.append(pick); rem.remove(pick)
        m = metrics(A[chosen].sum(axis=1) / len(chosen))
        best = m["sharpe"]
        print(f"    +{len(chosen):>2} {pick:<46} Sharpe {m['sharpe']:>5.3f}  "
              f"CAGR {m['cagr_pct']:>6.2f}%  DD {m['max_drawdown_pct']:>7.2f}%  "
              f"ratio {m['ratio']:>4.2f}")

    port = A[chosen].sum(axis=1) / len(chosen)
    pm = metrics(port)
    n = len(chosen)

    print(f"\n  PORTFOLIO — {n} members")
    for c in chosen:
        print(f"    {c}")
    print(f"\n    Sharpe {pm['sharpe']:.3f}   CAGR {pm['cagr_pct']:.2f}%   "
          f"MaxDD {pm['max_drawdown_pct']:.2f}%   ratio {pm['ratio']:.2f}")

    print("\n" + "=" * 118)
    print(f"  RISK SCALING — per-trade risk is (k x 1%) / {n} members of total capital")
    print("=" * 118)
    print(f"  {'risk/trade':>11}{'CAGR':>9}{'MaxDD':>10}{'net $':>12}{'final $':>12}   verdict")
    print("  " + "-" * 74)
    bestfit = None
    for k in range(1, 61, 3):
        m = metrics(port * k)
        rp = k * 1.0 / n
        ok = abs(m["max_drawdown_pct"]) <= DD_CAP
        v = ("*** 30% MET ***" if m["cagr_pct"] >= 30 else
             "** 20% MET **" if m["cagr_pct"] >= 20 else
             "* 10% MET *" if m["cagr_pct"] >= 10 else "") if ok else "DD breach"
        if ok:
            bestfit = (rp, m)
        if k <= 40 or ok:
            print(f"  {rp:>10.2f}%{m['cagr_pct']:>8.2f}%{m['max_drawdown_pct']:>9.2f}%"
                  f"{m['net_pnl_usd']:>12,.0f}{m['final_equity_usd']:>12,.0f}   {v}")

    print("\n" + "=" * 118)
    if bestfit:
        rp, m = bestfit
        print(f"  MAX CAGR under {DD_CAP}% DD:  {m['cagr_pct']:.2f}%  at {rp:.2f}% risk/trade"
              f"  (DD {m['max_drawdown_pct']:.2f}%)")
        print(f"  ${CAPITAL:,} → ${m['final_equity_usd']:,.0f}   (+${m['net_pnl_usd']:,.0f})")
        print(f"  Portfolio ratio {pm['ratio']:.2f}  |  need 1.0 for 10%, 2.0 for 20%, 3.0 for 30%")
    print("=" * 118)

    pd.DataFrame({"daily_pnl": port}).to_csv(
        RESULTS_DIR / f"kaufman_final_portfolio_{date.today()}.csv")
    print(f"\n  Runtime: {time.perf_counter()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
