import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Portfolio search — can we reach CAGR > 10% with MaxDD < 10%?
===============================================================
Consumes the out-of-sample survivors from run_full_screen.py and:

  1. Greedily builds the portfolio that maximises out-of-sample Sharpe
     (adding a strategy only if it actually improves the portfolio)
  2. Scales risk-per-trade to hit CAGR = 10% and reports the drawdown
     that comes with it — the honest answer to the target

Why scaling is the right final step: with fixed-fractional sizing, P&L
scales linearly with risk-per-trade, so CAGR and MaxDD move together. The
CAGR/DD RATIO is what a strategy actually gives you; risk sizing just picks
a point on that line. Target 10%/10% therefore needs ratio >= 1.0.

Run: python backtesting/book_strategies/kaufman/run_portfolio_search.py
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
from backtesting.book_strategies.kaufman.harness import run as run_harness, HARNESS_DEFAULTS
from backtesting.book_strategies.kaufman.strategies_adaptive import REGISTRY as R_ADAPT
from backtesting.book_strategies.kaufman.strategies_channels import (
    REGISTRY as R_CHAN, STRATEGY_CFG as CFG_CHAN)
from backtesting.book_strategies.kaufman.strategies_bulk import (
    REGISTRY as R_BULK, STRATEGY_CFG as CFG_BULK)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
CAPITAL = 20_000
RETAIL_COST = 0.35
OOS_START, OOS_END = "2003-05-05", "2019-08-23"

REGISTRY = {**R_BULK, **R_ADAPT, **R_CHAN}
PER_STRATEGY = {**CFG_BULK, **CFG_CHAN, "S092_AdaptiveBrkout": {"trailing": True}}

BASE_CFG = {
    "capital": CAPITAL, "risk_pct": 0.01, "max_position_pct": 0.50,
    "mode": "flat", "entry_on": "close", "default_stop_atr": 2.0,
    "cost_per_unit": RETAIL_COST,
}

TARGET_CAGR = 10.0
TARGET_DD   = 10.0

MIN_OOS_SHARPE = 0.28     # widened: no single peer to S019 exists, so test whether
MIN_OOS_RATIO  = 0.15     # many moderate uncorrelated strategies can combine instead
MAX_MEMBERS    = 10


def daily_stream(name, tf, loader) -> pd.Series:
    cfg = {**HARNESS_DEFAULTS, **BASE_CFG, **PER_STRATEGY.get(name, {})}
    df = loader(tf)
    sig = REGISTRY[name](df.copy(), cfg)
    trades = run_harness(df, sig, cfg)
    if not trades:
        return pd.Series(dtype=float)
    t = pd.DataFrame([x.to_dict() for x in trades])
    t["exit_time"] = pd.to_datetime(t["exit_time"])
    return t.groupby(t["exit_time"].dt.date)["pnl"].sum()


def metrics(daily: pd.Series, capital=CAPITAL) -> dict:
    if daily.empty or daily.std() == 0:
        return {"cagr_pct": 0, "sharpe": 0, "max_drawdown_pct": 0,
                "net_pnl_usd": 0, "final_equity_usd": capital, "ratio": 0}
    eq = capital + daily.cumsum()
    dd = ((eq - eq.cummax()) / eq.cummax() * 100).min()
    yrs = max((max(daily.index) - min(daily.index)).days, 1) / 365.25
    net = daily.sum()
    final = capital + net
    cagr = ((final / capital) ** (1 / yrs) - 1) * 100 if final > 0 else -100.0
    sharpe = daily.mean() / daily.std() * np.sqrt(252)
    return {"cagr_pct": cagr, "sharpe": sharpe, "max_drawdown_pct": dd,
            "net_pnl_usd": net, "final_equity_usd": final,
            "ratio": cagr / abs(dd) if dd else 0}


def main():
    t0 = time.perf_counter()
    files = sorted(RESULTS_DIR.glob("kaufman_full_screen_*.csv"))
    if not files:
        print("  No screen results found — run run_full_screen.py first.")
        return
    scr = pd.read_csv(files[-1])
    print("=" * 108)
    print(f"  PORTFOLIO SEARCH — target CAGR > {TARGET_CAGR}% with MaxDD < {TARGET_DD}%")
    print(f"  Source: {files[-1].name}")
    print("=" * 108)

    surv = scr[(scr["oos_net_pnl_usd"] > 0) & (scr["oos_profit_factor"] > 1.0) &
               (scr["oos_sharpe"] >= MIN_OOS_SHARPE) &
               (scr["oos_ratio"] >= MIN_OOS_RATIO)].copy()
    # keep the best timeframe per strategy so members stay distinct
    surv = surv.sort_values("oos_sharpe", ascending=False).drop_duplicates("strategy")
    print(f"\n  Candidates (OOS Sharpe >= {MIN_OOS_SHARPE}, unique strategies): {len(surv)}")
    for _, r in surv.iterrows():
        print(f"    {r['strategy']:<20}{r['timeframe']:>5}  OOS Sharpe {r['oos_sharpe']:>5.2f}  "
              f"CAGR {r['oos_cagr_pct']:>5.2f}%  DD {r['oos_max_drawdown_pct']:>7.2f}%  "
              f"ratio {r['oos_ratio']:>4.2f}")
    if surv.empty:
        print("  No candidates cleared the quality floor.")
        return

    # ── Build OOS daily streams ──────────────────────────────────────────
    print(f"\n  Building out-of-sample daily P&L streams...")
    loader_oos = lambda tf: get_bars_range(OOS_START, OOS_END, tf, "oos")
    streams = {}
    for _, r in surv.iterrows():
        key = f"{r['strategy']}@{r['timeframe']}"
        s = daily_stream(r["strategy"], r["timeframe"], loader_oos)
        if not s.empty:
            streams[key] = s
    idx = sorted(set().union(*[set(s.index) for s in streams.values()]))
    A = pd.DataFrame({k: v.reindex(idx, fill_value=0.0) for k, v in streams.items()}).sort_index()

    print(f"\n  Correlation matrix (OOS daily P&L)")
    print(A.corr().round(2).to_string())

    # ── Greedy forward selection on OOS Sharpe ───────────────────────────
    print(f"\n  Greedy portfolio construction (equal capital split among members)")
    chosen, best_sh = [], -99.0
    remaining = list(A.columns)
    while remaining and len(chosen) < MAX_MEMBERS:
        cand_best, cand_sh = None, best_sh
        for c in remaining:
            trial = chosen + [c]
            # equal capital: each member runs on CAPITAL/len(trial)
            port = A[trial].sum(axis=1) / len(trial)
            m = metrics(port)
            if m["sharpe"] > cand_sh:
                cand_best, cand_sh = c, m["sharpe"]
        if cand_best is None:
            break
        chosen.append(cand_best)
        remaining.remove(cand_best)
        port = A[chosen].sum(axis=1) / len(chosen)
        m = metrics(port)
        best_sh = m["sharpe"]
        print(f"    + {cand_best:<28} → members {len(chosen)}  Sharpe {m['sharpe']:>5.3f}  "
              f"CAGR {m['cagr_pct']:>5.2f}%  DD {m['max_drawdown_pct']:>7.2f}%  ratio {m['ratio']:>4.2f}")

    port = A[chosen].sum(axis=1) / len(chosen)
    pm = metrics(port)

    print(f"\n  BEST PORTFOLIO ({len(chosen)} members, ${CAPITAL:,} total, 1% risk each)")
    for c in chosen:
        print(f"    {c}")
    print(f"    CAGR {pm['cagr_pct']:.2f}%   Sharpe {pm['sharpe']:.3f}   "
          f"MaxDD {pm['max_drawdown_pct']:.2f}%   net ${pm['net_pnl_usd']:,.0f}   "
          f"final ${pm['final_equity_usd']:,.0f}")
    print(f"    CAGR/DD ratio: {pm['ratio']:.2f}   (need >= 1.0 for the 10%/10% target)")

    # ── Scale risk to hit the CAGR target ────────────────────────────────
    print(f"\n" + "=" * 108)
    print(f"  RISK SCALING — what does it take to reach CAGR {TARGET_CAGR}%?")
    print("=" * 108)
    print(f"  {'risk/trade':>11}{'CAGR':>9}{'MaxDD':>10}{'Sharpe':>9}{'net $':>12}{'final $':>12}   verdict")
    print("  " + "-" * 88)
    hit = None
    for k in [1, 2, 3, 4, 5, 6, 8, 10]:
        m = metrics(port * k)
        ok = m["cagr_pct"] >= TARGET_CAGR and abs(m["max_drawdown_pct"]) <= TARGET_DD
        verdict = "✅ TARGET MET" if ok else ("DD too high" if m["cagr_pct"] >= TARGET_CAGR else "")
        if ok and hit is None:
            hit = k
        print(f"  {k*1.0:>10.1f}%{m['cagr_pct']:>8.2f}%{m['max_drawdown_pct']:>9.2f}%"
              f"{m['sharpe']:>9.3f}{m['net_pnl_usd']:>12,.0f}{m['final_equity_usd']:>12,.0f}   {verdict}")

    print("\n" + "=" * 108)
    if hit:
        print(f"  ✅ TARGET ACHIEVABLE at {hit}% risk per trade.")
    else:
        need = TARGET_CAGR / TARGET_DD
        print(f"  ❌ TARGET NOT ACHIEVABLE with these strategies.")
        print(f"     Portfolio CAGR/DD ratio is {pm['ratio']:.2f}; the 10%/10% target needs >= {need:.2f}.")
        print(f"     Scaling risk moves BOTH numbers together — it cannot change the ratio.")
        k_needed = TARGET_CAGR / max(pm["cagr_pct"], 0.01)
        dd_at = abs(metrics(port * k_needed)["max_drawdown_pct"])
        print(f"     Reaching CAGR {TARGET_CAGR}% needs ~{k_needed:.1f}x risk → drawdown ≈ {dd_at:.0f}%.")
    print("=" * 108)

    pd.DataFrame({"portfolio_daily_pnl": port}).to_csv(
        RESULTS_DIR / f"kaufman_best_portfolio_oos_{date.today()}.csv")
    print(f"\n  Runtime: {time.perf_counter()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
