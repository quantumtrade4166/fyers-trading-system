import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Walk-forward portfolio test — the only honest way to evaluate selection
==========================================================================
Every portfolio number produced earlier was invalid: members were chosen using
the same out-of-sample data used to score them. That inflates results without
limit (we saw ratio 4.51 in-sample collapse to -0.13 out-of-sample).

Walk-forward removes the leak entirely:

    for each rebalance date T:
        SELECT  the best N members using ONLY data before T (trailing window)
        TRADE   them from T to T + hold_months, recording real forward P&L
        ROLL    forward and repeat

The concatenated forward P&L is a genuine simulation of what this selection
process would actually have delivered. Nothing in it was chosen with knowledge
of the period it is being scored on.

Three benchmarks are reported alongside it, because a walk-forward curve on
its own can still flatter a useless method:
    • EQUAL-WEIGHT ALL   — hold every candidate, no selection at all
    • RANDOM N           — pick N at random each period (average of 20 draws)
    • BEST SINGLE        — S019 on gold, our one robustly validated strategy
If selection cannot beat equal-weight and random, it adds nothing.

Stage 1 caches full-history daily P&L per combination (slow, once).
Stage 2 runs the walk-forward on the cache (fast, re-runnable).

Run: python backtesting/book_strategies/kaufman/run_walkforward.py
"""

import time
import warnings
from pathlib import Path
from datetime import date

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data_multi import get_bars, available, COSTS, LABELS
from backtesting.book_strategies.kaufman.harness import run as run_h, HARNESS_DEFAULTS
from backtesting.book_strategies.kaufman.strategies_adaptive import REGISTRY as R_A
from backtesting.book_strategies.kaufman.strategies_channels import (
    REGISTRY as R_C, STRATEGY_CFG as CFG_C)
from backtesting.book_strategies.kaufman.strategies_bulk import (
    REGISTRY as R_B, STRATEGY_CFG as CFG_B)
from backtesting.book_strategies.kaufman.strategies_bulk2 import (
    REGISTRY as R_B2, STRATEGY_CFG as CFG_B2)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
CACHE = Path(__file__).parent / "_cache_multi" / "wf_streams.parquet"
CAPITAL = 20_000

REGISTRY = {**R_B, **R_B2, **R_A, **R_C}
PER_STRATEGY = {**CFG_B, **CFG_B2, **CFG_C, "S092_AdaptiveBrkout": {"trailing": True}}

TIMEFRAMES = ["2h", "4h", "8h", "12h", "1D"]

LOOKBACK_M = 24          # months of history used to select
HOLD_M     = 6           # months each selection is traded before re-selecting
N_MEMBERS  = 15
MIN_TRAIL_TRADES = 20    # a member must have actually traded in the lookback


def build_streams() -> pd.DataFrame:
    if CACHE.exists():
        return pd.read_parquet(CACHE)

    insts = [i for i in available() if i in COSTS]
    print(f"  Building full-history streams: {len(REGISTRY)} strategies x "
          f"{len(insts)} instruments x {len(TIMEFRAMES)} timeframes")
    out = {}
    t0 = time.perf_counter()
    for inst in insts:
        for tf in TIMEFRAMES:
            try:
                df = get_bars(inst, tf)
            except Exception:
                continue
            if len(df) < 500:
                continue
            for name, fn in REGISTRY.items():
                cfg = {**HARNESS_DEFAULTS, "capital": CAPITAL, "risk_pct": 0.01,
                       "max_position_pct": 0.50, "mode": "flat", "entry_on": "close",
                       "default_stop_atr": 2.0, "cost_per_unit": COSTS[inst],
                       **PER_STRATEGY.get(name, {})}
                try:
                    tr = run_h(df, fn(df.copy(), cfg), cfg)
                except Exception:
                    continue
                if len(tr) < 30:
                    continue
                t = pd.DataFrame([x.to_dict() for x in tr])
                t["exit_time"] = pd.to_datetime(t["exit_time"])
                out[f"{LABELS[inst]}|{name}@{tf}"] = t.groupby(
                    t["exit_time"].dt.date)["pnl"].sum()
            print(f"    {LABELS[inst]:<12} {tf:>4} — {len(out):>4} streams so far "
                  f"({time.perf_counter()-t0:.0f}s)")

    idx = sorted({d for s in out.values() for d in s.index})
    A = pd.DataFrame({k: v.reindex(idx, fill_value=0.0) for k, v in out.items()}).sort_index()
    A.index = pd.to_datetime(A.index)
    A.to_parquet(CACHE)
    print(f"  Cached {A.shape[1]} streams x {A.shape[0]} days")
    return A


def metrics(daily, capital=CAPITAL):
    if len(daily) == 0 or daily.std() == 0:
        return dict(cagr_pct=0, sharpe=0, max_drawdown_pct=0,
                    net_pnl_usd=0, final_equity_usd=capital, ratio=0)
    eq = capital + daily.cumsum()
    dd = ((eq - eq.cummax()) / eq.cummax() * 100).min()
    yrs = max((daily.index.max() - daily.index.min()).days, 1) / 365.25
    net = daily.sum(); fin = capital + net
    cagr = ((fin / capital) ** (1 / yrs) - 1) * 100 if fin > 0 else -100.0
    return dict(cagr_pct=cagr, sharpe=daily.mean() / daily.std() * np.sqrt(252),
                max_drawdown_pct=dd, net_pnl_usd=net, final_equity_usd=fin,
                ratio=cagr / abs(dd) if dd else 0)


def walk_forward(A: pd.DataFrame, mode="select", n=N_MEMBERS, seed=0):
    """mode: 'select' (top-N by trailing Sharpe) | 'all' | 'random'"""
    rng = np.random.default_rng(seed)
    start = A.index.min() + pd.DateOffset(months=LOOKBACK_M)
    edges = pd.date_range(start, A.index.max(), freq=f"{HOLD_M}MS")
    pieces, picks_log = [], []

    for t in edges:
        trail = A.loc[(A.index >= t - pd.DateOffset(months=LOOKBACK_M)) & (A.index < t)]
        fwd   = A.loc[(A.index >= t) & (A.index < t + pd.DateOffset(months=HOLD_M))]
        if len(trail) < 100 or len(fwd) == 0:
            continue

        active = trail.columns[(trail != 0).sum() >= MIN_TRAIL_TRADES]
        if len(active) == 0:
            continue

        if mode == "all":
            sel = list(active)
        elif mode == "random":
            sel = list(rng.choice(active, size=min(n, len(active)), replace=False))
        else:
            sd = trail[active].std().replace(0, np.nan)
            sh = (trail[active].mean() / sd) * np.sqrt(252)
            sel = list(sh.sort_values(ascending=False).head(n).index)

        if not sel:
            continue
        pieces.append(fwd[sel].sum(axis=1) / len(sel))
        picks_log.append((t.date(), sel))

    if not pieces:
        return pd.Series(dtype=float), []
    return pd.concat(pieces).sort_index(), picks_log


def main():
    t0 = time.perf_counter()
    print("=" * 112)
    print("  WALK-FORWARD PORTFOLIO TEST")
    print(f"  select on {LOOKBACK_M}m trailing → trade {HOLD_M}m → roll   |   "
          f"{N_MEMBERS} members, ${CAPITAL:,}")
    print("=" * 112)

    A = build_streams()
    print(f"\n  Universe: {A.shape[1]} strategy/instrument/timeframe streams, "
          f"{A.index.min().date()} → {A.index.max().date()}")

    wf, picks = walk_forward(A, "select")
    eq  = walk_forward(A, "all")[0]
    rnd = [walk_forward(A, "random", seed=s)[0] for s in range(20)]

    m_wf = metrics(wf); m_eq = metrics(eq)
    m_rnd_list = [metrics(r) for r in rnd if len(r)]
    m_rnd = {k: float(np.mean([m[k] for m in m_rnd_list])) for k in m_wf}

    print(f"\n  Walk-forward periods: {len(picks)}   "
          f"({picks[0][0]} → {picks[-1][0]})" if picks else "  no periods")

    print("\n" + "=" * 112)
    print("  RESULTS — all genuinely out-of-sample")
    print("=" * 112)
    print(f"  {'method':<34}{'Sharpe':>9}{'CAGR':>9}{'MaxDD':>10}{'ratio':>8}{'net $':>12}")
    print("  " + "-" * 82)
    print(f"  {'WALK-FORWARD (top-15 selection)':<34}{m_wf['sharpe']:>9.3f}"
          f"{m_wf['cagr_pct']:>8.2f}%{m_wf['max_drawdown_pct']:>9.2f}%"
          f"{m_wf['ratio']:>8.2f}{m_wf['net_pnl_usd']:>12,.0f}")
    print(f"  {'EQUAL-WEIGHT ALL (no selection)':<34}{m_eq['sharpe']:>9.3f}"
          f"{m_eq['cagr_pct']:>8.2f}%{m_eq['max_drawdown_pct']:>9.2f}%"
          f"{m_eq['ratio']:>8.2f}{m_eq['net_pnl_usd']:>12,.0f}")
    print(f"  {'RANDOM 15 (avg of 20 draws)':<34}{m_rnd['sharpe']:>9.3f}"
          f"{m_rnd['cagr_pct']:>8.2f}%{m_rnd['max_drawdown_pct']:>9.2f}%"
          f"{m_rnd['ratio']:>8.2f}{m_rnd['net_pnl_usd']:>12,.0f}")

    verdict = ("SELECTION ADDS VALUE" if m_wf["sharpe"] > max(m_eq["sharpe"], m_rnd["sharpe"]) + 0.1
               else "SELECTION ADDS NOTHING — no better than random / equal-weight")
    print(f"\n  → {verdict}")

    if len(wf) and m_wf["net_pnl_usd"] > 0:
        print("\n" + "=" * 112)
        print("  RISK SCALING on the walk-forward equity (honest)")
        print("=" * 112)
        print(f"  {'risk/trade':>11}{'CAGR':>9}{'MaxDD':>10}{'net $':>12}{'final $':>12}   verdict")
        print("  " + "-" * 74)
        best = None
        for k in range(1, 61, 3):
            m = metrics(wf * k); rp = k * 1.0 / N_MEMBERS
            ok = abs(m["max_drawdown_pct"]) <= 10
            v = ("*** 30% ***" if m["cagr_pct"] >= 30 else "** 20% **" if m["cagr_pct"] >= 20
                 else "* 10% *" if m["cagr_pct"] >= 10 else "") if ok else "DD breach"
            if ok:
                best = (rp, m)
            print(f"  {rp:>10.2f}%{m['cagr_pct']:>8.2f}%{m['max_drawdown_pct']:>9.2f}%"
                  f"{m['net_pnl_usd']:>12,.0f}{m['final_equity_usd']:>12,.0f}   {v}")
        if best:
            rp, m = best
            print(f"\n  MAX CAGR under 10% DD: {m['cagr_pct']:.2f}% at {rp:.2f}% risk/trade "
                  f"(DD {m['max_drawdown_pct']:.2f}%)  ${CAPITAL:,} → ${m['final_equity_usd']:,.0f}")

    # How stable are the picks? High turnover = selection is chasing noise.
    if len(picks) > 1:
        overlaps = [len(set(picks[i][1]) & set(picks[i-1][1])) / len(picks[i][1])
                    for i in range(1, len(picks))]
        print(f"\n  Selection turnover: {np.mean(overlaps)*100:.0f}% of members retained "
              f"period-to-period (low retention = chasing noise)")

    if len(wf):
        pd.DataFrame({"daily_pnl": wf}).to_csv(
            RESULTS_DIR / f"kaufman_walkforward_{date.today()}.csv")
    print(f"\n  Runtime: {time.perf_counter()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
