import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Cross-instrument portfolio search
====================================
Combines out-of-sample survivors across INSTRUMENTS, strategies and timeframes
into one account, then finds the risk level that maximises CAGR subject to a
hard drawdown cap.

Diversifying across instruments is the lever that diversifying across
strategies on a single instrument could not provide: nine strategies on gold
are nine views of one price series (ratio capped ~0.47), whereas trend systems
on indices, energy, metals and FX depend on genuinely different drivers.

Selection is greedy on out-of-sample Sharpe, which is far more stable than
greedy on ratio (MaxDD is a single-point statistic and overfits easily).

Run: python backtesting/book_strategies/kaufman/run_multi_portfolio.py
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
from backtesting.book_strategies.kaufman.data import get_bars_range as gold_bars
from backtesting.book_strategies.kaufman.harness import run as run_harness, HARNESS_DEFAULTS
from backtesting.book_strategies.kaufman.strategies_adaptive import REGISTRY as R_ADAPT
from backtesting.book_strategies.kaufman.strategies_channels import (
    REGISTRY as R_CHAN, STRATEGY_CFG as CFG_CHAN)
from backtesting.book_strategies.kaufman.strategies_bulk import (
    REGISTRY as R_BULK, STRATEGY_CFG as CFG_BULK)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
CAPITAL = 20_000
SPLIT, OOS_BEG = "2019-08-23", "2011-09-01"

REGISTRY = {**R_BULK, **R_ADAPT, **R_CHAN}
PER_STRATEGY = {**CFG_BULK, **CFG_CHAN, "S092_AdaptiveBrkout": {"trailing": True}}

DD_CAP = 10.0
MAX_MEMBERS = 12
MIN_OOS_SHARPE = 0.25
MIN_OOS_RATIO  = 0.12
MAX_PER_STRATEGY = 2      # a strategy may appear on at most 2 instruments
# Guard against the low-trade artifact: PF 15 on 9 trades is noise, not edge
MIN_OOS_TRADES = 50


def stream(inst, name, tf, start, end):
    cfg = {**HARNESS_DEFAULTS, "capital": CAPITAL, "risk_pct": 0.01,
           "max_position_pct": 0.50, "mode": "flat", "entry_on": "close",
           "default_stop_atr": 2.0, "cost_per_unit": COSTS[inst],
           **PER_STRATEGY.get(name, {})}
    if inst == "xauusd":
        # gold lives in the 1-second archive with its own cached loader
        df = gold_bars("2003-05-05", "2019-08-23", tf, "oos")
        df = df[(df.index >= start) & (df.index <= end)]
    else:
        df = get_bars(inst, tf, start=start, end=end)
    sig = REGISTRY[name](df.copy(), cfg)
    tr = run_harness(df, sig, cfg)
    if not tr:
        return pd.Series(dtype=float)
    t = pd.DataFrame([x.to_dict() for x in tr])
    t["exit_time"] = pd.to_datetime(t["exit_time"])
    return t.groupby(t["exit_time"].dt.date)["pnl"].sum()


def metrics(daily, capital=CAPITAL):
    if daily.empty or daily.std() == 0:
        return {"cagr_pct": 0, "sharpe": 0, "max_drawdown_pct": 0,
                "net_pnl_usd": 0, "final_equity_usd": capital, "ratio": 0}
    eq = capital + daily.cumsum()
    dd = ((eq - eq.cummax()) / eq.cummax() * 100).min()
    yrs = max((max(daily.index) - min(daily.index)).days, 1) / 365.25
    net = daily.sum()
    final = capital + net
    cagr = ((final / capital) ** (1 / yrs) - 1) * 100 if final > 0 else -100.0
    return {"cagr_pct": cagr, "sharpe": daily.mean() / daily.std() * np.sqrt(252),
            "max_drawdown_pct": dd, "net_pnl_usd": net, "final_equity_usd": final,
            "ratio": cagr / abs(dd) if dd else 0}


def main():
    t0 = time.perf_counter()
    files = sorted(RESULTS_DIR.glob("kaufman_index_hunt_*.csv"))
    if not files:
        print("  Run run_index_hunt.py first.")
        return
    res = pd.read_csv(files[-1])

    print("=" * 116)
    print("  CROSS-INSTRUMENT PORTFOLIO SEARCH")
    print(f"  Source: {files[-1].name}  |  ${CAPITAL:,} total  |  DD cap {DD_CAP}%")
    print("=" * 116)

    # Fold in the gold survivors so the portfolio is genuinely cross-instrument
    gfiles = sorted(RESULTS_DIR.glob("kaufman_full_screen_*.csv"))
    if gfiles:
        g = pd.read_csv(gfiles[-1])
        g["instrument"] = "xauusd"; g["label"] = "Gold"
        if "oos_ratio" not in g.columns:
            g["oos_ratio"] = g["oos_cagr_pct"] / g["oos_max_drawdown_pct"].abs()
        res = pd.concat([res, g], ignore_index=True)
        print(f"  Pool includes {len(g)} gold combinations")

    cand = res[(res["oos_net_pnl_usd"] > 0) & (res["oos_profit_factor"] > 1.0) &
               (res["oos_sharpe"] >= MIN_OOS_SHARPE) &
               (res["oos_ratio"] >= MIN_OOS_RATIO) &
               (res["oos_total_trades"] >= MIN_OOS_TRADES)].copy()
    cand = cand.sort_values("oos_sharpe", ascending=False)
    cand = cand.groupby(["instrument", "strategy"]).head(1)          # best TF each
    cand = cand.groupby("strategy").head(MAX_PER_STRATEGY)           # limit repeats
    cand = cand.head(30)
    print(f"\n  Candidate pool: {len(cand)}")
    for _, r in cand.iterrows():
        print(f"    {r['label']:<11} {r['strategy']:<20}{r['timeframe']:>6}  "
              f"Sharpe {r['oos_sharpe']:>5.2f}  CAGR {r['oos_cagr_pct']:>6.2f}%  "
              f"DD {r['oos_max_drawdown_pct']:>7.2f}%  ratio {r['oos_ratio']:>4.2f}")
    if cand.empty:
        print("  No candidates cleared the floor.")
        return

    print("\n  Building out-of-sample streams...")
    streams = {}
    for _, r in cand.iterrows():
        k = f"{LABELS[r['instrument']]}|{r['strategy']}@{r['timeframe']}"
        s = stream(r["instrument"], r["strategy"], r["timeframe"], OOS_BEG, SPLIT)
        if not s.empty:
            streams[k] = s
    idx = sorted(set().union(*[set(s.index) for s in streams.values()]))
    A = pd.DataFrame({k: v.reindex(idx, fill_value=0.0) for k, v in streams.items()}).sort_index()

    corr = A.corr()
    off = corr.to_numpy()[np.triu_indices(len(corr), k=1)]
    print(f"\n  Average pairwise correlation: {off.mean():.3f} "
          f"(min {off.min():.3f}, max {off.max():.3f})")

    # ── Greedy on Sharpe ─────────────────────────────────────────────────
    print(f"\n  Greedy construction (equal capital, 1% risk each)")
    chosen, best = [], -99.0
    rem = list(A.columns)
    while rem and len(chosen) < MAX_MEMBERS:
        pick, pick_sh = None, best
        for c in rem:
            m = metrics(A[chosen + [c]].sum(axis=1) / (len(chosen) + 1))
            if m["sharpe"] > pick_sh:
                pick, pick_sh = c, m["sharpe"]
        if pick is None:
            break
        chosen.append(pick); rem.remove(pick)
        m = metrics(A[chosen].sum(axis=1) / len(chosen))
        best = m["sharpe"]
        print(f"    +{len(chosen):>2} {pick:<44} Sharpe {m['sharpe']:>5.3f}  "
              f"CAGR {m['cagr_pct']:>6.2f}%  DD {m['max_drawdown_pct']:>7.2f}%  "
              f"ratio {m['ratio']:>4.2f}")

    port = A[chosen].sum(axis=1) / len(chosen)
    pm = metrics(port)

    print(f"\n  PORTFOLIO ({len(chosen)} members) — out-of-sample {OOS_BEG} → {SPLIT}")
    print(f"    Sharpe {pm['sharpe']:.3f}  CAGR {pm['cagr_pct']:.2f}%  "
          f"MaxDD {pm['max_drawdown_pct']:.2f}%  ratio {pm['ratio']:.2f}")

    # ── Risk scaling under the DD cap ────────────────────────────────────
    print("\n" + "=" * 116)
    print(f"  RISK SCALING — maximise CAGR subject to MaxDD <= {DD_CAP}%")
    print("=" * 116)
    print(f"  {'risk/trade':>11}{'CAGR':>9}{'MaxDD':>10}{'net $':>12}{'final $':>12}   verdict")
    print("  " + "-" * 76)
    bestk = None
    for k in [0.25, 0.5, 0.75, 1, 1.5, 2, 2.5, 3, 4, 5]:
        m = metrics(port * k)
        ok = abs(m["max_drawdown_pct"]) <= DD_CAP
        if ok:
            bestk = (k, m)
        tag = ""
        if ok and m["cagr_pct"] >= 25:  tag = "TARGET 25%+ MET"
        elif ok and m["cagr_pct"] >= 10: tag = "TARGET 10%+ MET"
        elif not ok: tag = "DD breach"
        print(f"  {k*1.0:>10.2f}%{m['cagr_pct']:>8.2f}%{m['max_drawdown_pct']:>9.2f}%"
              f"{m['net_pnl_usd']:>12,.0f}{m['final_equity_usd']:>12,.0f}   {tag}")

    print("\n" + "=" * 116)
    if bestk:
        k, m = bestk
        print(f"  Max CAGR while holding DD <= {DD_CAP}%:  {m['cagr_pct']:.2f}% "
              f"at {k:.2f}% risk/trade  (DD {m['max_drawdown_pct']:.2f}%)")
        if m["cagr_pct"] >= 25:
            print("  ✅ 25% target ACHIEVED out-of-sample")
        elif m["cagr_pct"] >= 10:
            print("  ✅ 10% target achieved; 25% not reached")
        else:
            print(f"  ❌ Falls short of 10%. Portfolio ratio {pm['ratio']:.2f}; "
                  f"need 1.0 for 10%/10%, 2.5 for 25%/10%.")
    print("=" * 116)

    pd.DataFrame({"daily_pnl": port}).to_csv(
        RESULTS_DIR / f"kaufman_multi_portfolio_oos_{date.today()}.csv")
    A.to_csv(RESULTS_DIR / f"kaufman_multi_streams_oos_{date.today()}.csv")
    print(f"\n  Runtime: {time.perf_counter()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
