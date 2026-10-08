import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Honest portfolio construction — no out-of-sample leakage
===========================================================
The previous portfolio search reported Sharpe 2.53 / ratio 4.34. That number
is NOT trustworthy: members were greedily chosen to maximise Sharpe *on the
out-of-sample data itself*. With 893 candidates, picking the 20 that happen to
combine well on one specific period is curve-fitting — the OOS window had been
consumed by the selection step, so it no longer validated anything.

This does it correctly:
    SELECT  members using IN-SAMPLE data only (2019-08 → present)
    FREEZE  the member list
    EVALUATE that frozen portfolio on OUT-OF-SAMPLE data (→ 2019-08)

The out-of-sample number this produces is an honest estimate of what the
selection process would have delivered on unseen data.

Run: python backtesting/book_strategies/kaufman/run_honest_portfolio.py
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
MIN_IS_SHARPE = 0.25
MIN_IS_TRADES = 60
MAX_PER_STRATEGY = 3
MAX_PER_INSTRUMENT = 4


def stream(inst, name, tf, start=None, end=None):
    cfg = {**HARNESS_DEFAULTS, "capital": CAPITAL, "risk_pct": 0.01,
           "max_position_pct": 0.50, "mode": "flat", "entry_on": "close",
           "default_stop_atr": 2.0, "cost_per_unit": COSTS[inst],
           **PER_STRATEGY.get(name, {})}
    df = get_bars(inst, tf, start=start, end=end)
    if len(df) < 200:
        return pd.Series(dtype=float)
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


def align(streams, common_start=True):
    if not streams:
        return pd.DataFrame()
    if common_start:
        start = max(min(s.index) for s in streams.values())
        idx = sorted({d for s in streams.values() for d in s.index if d >= start})
    else:
        idx = sorted({d for s in streams.values() for d in s.index})
    return pd.DataFrame({k: v.reindex(idx, fill_value=0.0)
                         for k, v in streams.items()}).sort_index()


def main():
    t0 = time.perf_counter()
    files = sorted(RESULTS_DIR.glob("kaufman_mega_hunt_*.csv"))
    if not files:
        print("  Run run_mega_hunt.py first."); return
    res = pd.read_csv(files[-1])

    print("=" * 116)
    print("  HONEST PORTFOLIO — members selected on IN-SAMPLE only, tested out-of-sample")
    print(f"  Source: {files[-1].name}  |  ${CAPITAL:,}  |  DD cap {DD_CAP}%")
    print("=" * 116)

    # ── Candidates ranked by IN-SAMPLE quality only ──────────────────────
    cand = res[(res.is_net_pnl_usd > 0) & (res.is_profit_factor > 1.0) &
               (res.is_sharpe >= MIN_IS_SHARPE) &
               (res.is_total_trades >= MIN_IS_TRADES)].copy()
    cand = cand.sort_values("is_sharpe", ascending=False)
    cand = cand.groupby(["instrument", "strategy"]).head(1)
    cand = cand.groupby("strategy").head(MAX_PER_STRATEGY)
    cand = cand.groupby("instrument").head(MAX_PER_INSTRUMENT)
    cand = cand.head(40)
    print(f"\n  Candidate pool (IN-SAMPLE criteria only): {len(cand)}")

    print("  Building in-sample streams for selection...")
    is_streams, keys = {}, {}
    for _, r in cand.iterrows():
        k = f"{r.label}|{r.strategy}@{r.timeframe}"
        s = stream(r.instrument, r.strategy, r.timeframe, start=SPLIT)
        if len(s) >= 30:
            is_streams[k] = s
            keys[k] = (r.instrument, r.strategy, r.timeframe)
    A_is = align(is_streams)
    print(f"  {len(is_streams)} streams | selection window {min(A_is.index)} → {max(A_is.index)}")

    # ── Greedy selection ON IN-SAMPLE ────────────────────────────────────
    print("\n  Greedy selection (in-sample Sharpe):")
    chosen, best = [], -99.0
    rem = list(A_is.columns)
    while rem and len(chosen) < MAX_MEMBERS:
        pick, ps = None, best
        for c in rem:
            m = metrics(A_is[chosen + [c]].sum(axis=1) / (len(chosen) + 1))
            if m["sharpe"] > ps:
                pick, ps = c, m["sharpe"]
        if pick is None:
            break
        chosen.append(pick); rem.remove(pick)
    m_is = metrics(A_is[chosen].sum(axis=1) / len(chosen))
    print(f"    selected {len(chosen)} members | IN-SAMPLE Sharpe {m_is['sharpe']:.3f}  "
          f"CAGR {m_is['cagr_pct']:.2f}%  DD {m_is['max_drawdown_pct']:.2f}%  "
          f"ratio {m_is['ratio']:.2f}   <-- optimistic by construction")

    # ── FREEZE and evaluate on OUT-OF-SAMPLE ─────────────────────────────
    print("\n  Frozen member list:")
    for c in chosen:
        print(f"    {c}")

    print("\n  Building out-of-sample streams for the SAME members...")
    oos_streams = {}
    for c in chosen:
        inst, name, tf = keys[c]
        s = stream(inst, name, tf, end=SPLIT)
        if len(s) >= 30:
            oos_streams[c] = s
    A_oos = align(oos_streams)
    n = len(oos_streams)
    port = A_oos.sum(axis=1) / n
    m_oos = metrics(port)
    print(f"  {n} members had OOS data | window {min(A_oos.index)} → {max(A_oos.index)}")

    corr = A_oos.corr()
    off = corr.to_numpy()[np.triu_indices(len(corr), k=1)]

    print("\n" + "=" * 116)
    print("  THE HONEST COMPARISON")
    print("=" * 116)
    print(f"  {'window':<34}{'Sharpe':>9}{'CAGR':>9}{'MaxDD':>10}{'ratio':>8}")
    print("  " + "-" * 68)
    print(f"  {'IN-SAMPLE (used for selection)':<34}{m_is['sharpe']:>9.3f}"
          f"{m_is['cagr_pct']:>8.2f}%{m_is['max_drawdown_pct']:>9.2f}%{m_is['ratio']:>8.2f}")
    print(f"  {'OUT-OF-SAMPLE (never seen)':<34}{m_oos['sharpe']:>9.3f}"
          f"{m_oos['cagr_pct']:>8.2f}%{m_oos['max_drawdown_pct']:>9.2f}%{m_oos['ratio']:>8.2f}")
    decay = (m_oos['ratio'] / m_is['ratio'] - 1) * 100 if m_is['ratio'] else 0
    print(f"\n  Ratio decay from selection bias: {decay:+.0f}%")
    print(f"  Average pairwise correlation (OOS): {off.mean():.3f}")

    print("\n" + "=" * 116)
    print(f"  RISK SCALING on the HONEST out-of-sample portfolio")
    print("=" * 116)
    print(f"  {'risk/trade':>11}{'CAGR':>9}{'MaxDD':>10}{'net $':>12}{'final $':>12}   verdict")
    print("  " + "-" * 74)
    bestfit = None
    for k in range(1, 81, 4):
        m = metrics(port * k)
        rp = k * 1.0 / n
        ok = abs(m["max_drawdown_pct"]) <= DD_CAP
        v = ("*** 30% ***" if m["cagr_pct"] >= 30 else
             "** 20% **" if m["cagr_pct"] >= 20 else
             "* 10% *" if m["cagr_pct"] >= 10 else "") if ok else "DD breach"
        if ok:
            bestfit = (rp, m)
        print(f"  {rp:>10.2f}%{m['cagr_pct']:>8.2f}%{m['max_drawdown_pct']:>9.2f}%"
              f"{m['net_pnl_usd']:>12,.0f}{m['final_equity_usd']:>12,.0f}   {v}")

    print("\n" + "=" * 116)
    if bestfit:
        rp, m = bestfit
        print(f"  HONEST MAX CAGR under {DD_CAP}% DD: {m['cagr_pct']:.2f}% at {rp:.2f}% risk/trade "
              f"(DD {m['max_drawdown_pct']:.2f}%)")
        print(f"  ${CAPITAL:,} → ${m['final_equity_usd']:,.0f}")
    print("=" * 116)

    pd.DataFrame({"daily_pnl": port}).to_csv(
        RESULTS_DIR / f"kaufman_honest_portfolio_{date.today()}.csv")
    print(f"\n  Runtime: {time.perf_counter()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
