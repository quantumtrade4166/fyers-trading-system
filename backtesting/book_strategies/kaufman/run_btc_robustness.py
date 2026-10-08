import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
BTC robustness — do the screen's top candidates survive being poked?
=======================================================================
Follows run_btc_screen.py. Six tests:

  1. PARAMETER NEIGHBOURHOOD — 30 random joint perturbations (each numeric
     param x U[0.6, 1.4]). A real edge is a plateau; a fitted one is a spike.
     Reports % of neighbours profitable OOS and where the default ranks.
  2. TIMEFRAME NEIGHBOURS — same strategy/mode one step either side (from
     the screen CSV).
  3. YEAR BY YEAR — net P&L per calendar year 2018-2026, count losing years.
  4. COST STRESS — 2x fees and 20% APR funding, OOS.
  5. VENUE CHECK — same rules on Delta Exchange BTCUSD perp prices vs Binance,
     2025-01 -> now (2024 Delta data is thin and noisy, see data check).
  6. WALK-FORWARD SELECTION — the test that caught the gold portfolio. Every
     6 months pick the top 10 combos by trailing-24-month Sharpe from the
     whole 4h+ universe, hold them 6 months, roll. Compare against random 10
     (100 draws) and equal-weight-everything. If picking can't beat random,
     picking adds nothing and the screen's winners are luck.

Run: .venv\\Scripts\\python.exe backtesting\\book_strategies\\kaufman\\run_btc_robustness.py
"""

import inspect
import re
import time
import warnings
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data_btc import get_bars, delta_perp_1h, DELTA_COST_PCT
from backtesting.book_strategies.kaufman.harness import run, metrics
from backtesting.book_strategies.kaufman.run_btc_screen import (
    REGISTRY, PER_STRATEGY, BASE_CFG, CAPITAL, SPLIT, TIMEFRAMES, RESULTS_DIR)

CANDIDATES = [
    ("S019_LinRegBreak", "4h", "ls"), ("S019_LinRegBreak", "4h", "lo"),
    ("S052_MACD", "8h", "ls"),        ("S052_MACD", "12h", "ls"),
    ("S024_BBBreak", "4h", "ls"),     ("S060_CCI", "4h", "lo"),
    ("S060_CCI", "8h", "ls"),         ("S026_ATRBands", "4h", "ls"),
    ("S039_MASequence", "4h", "lo"),  ("S023_KeltnerBreak", "12h", "ls"),
    ("S118_ATRVolBrk", "12h", "ls"),  ("S033_SingleMA", "8h", "ls"),
]
N_NEIGHBOURS = 30
WF_UNIVERSE_TFS = ["4h", "8h", "12h", "1D"]
WF_START, WF_TRAIN_M, WF_HOLD_M, WF_TOP = "2020-01-01", 24, 6, 10
DELTA_FROM = "2025-01-01"


# ── core helpers ─────────────────────────────────────────────────────────
def signals_for(df, name, mode, cfg):
    sig = REGISTRY[name](df.copy(), cfg)
    if mode == "lo":
        sig = sig.copy()
        entry = sig["entry"].fillna(0)
        if cfg.get("mode") == "reversal":
            sig["exit_long"] = sig["exit_long"].fillna(False).astype(bool) | (entry < 0)
            cfg = {**cfg, "mode": "flat"}
        sig["entry"] = entry.clip(lower=0)
    return sig, cfg


def trades_for(df, name, mode, overrides=None):
    cfg = {**BASE_CFG, **PER_STRATEGY.get(name, {}), **(overrides or {})}
    sig, cfg = signals_for(df, name, mode, cfg)
    return run(df, sig, cfg)


def m_of(trades, start=None, end=None):
    t = [x for x in trades
         if (start is None or x.entry_time >= pd.Timestamp(start))
         and (end is None or x.entry_time < pd.Timestamp(end))]
    return metrics(t, CAPITAL, calendar="calendar")


def default_params(name):
    src = inspect.getsource(REGISTRY[name])
    return {k: float(v) for k, v in re.findall(r'cfg\.get\(\s*"(\w+)"\s*,\s*([-\d.]+)', src)}


def ratio(m):
    dd = abs(m.get("max_drawdown_pct", 0))
    return m.get("cagr_pct", 0) / dd if dd > 0.01 else 0.0


# ── test 1: neighbourhood (+ tests 3, 4 on the default) ─────────────────
def candidate_job(name, tf, mode):
    df = get_bars(tf)
    base = trades_for(df, name, mode)
    m_is, m_oos = m_of(base, end=SPLIT), m_of(base, start=SPLIT)

    rng = np.random.default_rng(abs(hash((name, tf, mode))) % 2**32)
    params = default_params(name)
    neigh = []
    for _ in range(N_NEIGHBOURS if params else 0):
        ov = {}
        for k, v in params.items():
            x = v * rng.uniform(0.6, 1.4)
            ov[k] = max(2, int(round(x))) if float(v).is_integer() and v >= 2 else round(x, 3)
        if "fast" in ov and "slow" in ov and ov["fast"] >= ov["slow"]:
            continue
        try:
            tr = trades_for(df, name, mode, ov)
        except Exception:
            continue
        neigh.append({"is": m_of(tr, end=SPLIT), "oos": m_of(tr, start=SPLIT)})

    # year by year (default params, full history)
    tdf = pd.DataFrame([t.to_dict() for t in base])
    yearly = (tdf.assign(y=pd.to_datetime(tdf.exit_time).dt.year)
                 .groupby("y")["pnl"].sum().round(0).to_dict()) if len(tdf) else {}

    # cost stress
    stress = trades_for(df, name, mode, {"cost_pct": DELTA_COST_PCT * 2, "funding_apr": 0.20})
    m_stress = m_of(stress, start=SPLIT)

    oos_sh = [n["oos"]["sharpe"] for n in neigh]
    oos_pnl = [n["oos"]["net_pnl_usd"] for n in neigh]
    is_pnl = [n["is"]["net_pnl_usd"] for n in neigh]
    return {
        "strategy": name, "timeframe": tf, "mode": mode,
        "is_sharpe": m_is["sharpe"], "oos_sharpe": m_oos["sharpe"],
        "oos_cagr": m_oos["cagr_pct"], "oos_dd": m_oos["max_drawdown_pct"],
        "oos_ratio": round(ratio(m_oos), 2), "oos_pnl": m_oos["net_pnl_usd"],
        "oos_trades": m_oos["total_trades"],
        "n_neigh": len(neigh),
        "neigh_oos_profitable_pct": round(100 * np.mean([p > 0 for p in oos_pnl]), 0) if neigh else np.nan,
        "neigh_is_profitable_pct": round(100 * np.mean([p > 0 for p in is_pnl]), 0) if neigh else np.nan,
        "neigh_oos_sharpe_median": round(float(np.median(oos_sh)), 2) if neigh else np.nan,
        "neigh_oos_sharpe_p10": round(float(np.percentile(oos_sh, 10)), 2) if neigh else np.nan,
        "default_oos_pctile": round(100 * np.mean([s < m_oos["sharpe"] for s in oos_sh]), 0) if neigh else np.nan,
        "losing_years": sum(1 for y, v in yearly.items() if 2018 <= y <= 2025 and v < 0),
        "yearly": yearly,
        "stress_oos_pnl": m_stress["net_pnl_usd"], "stress_oos_sharpe": m_stress["sharpe"],
    }


# ── test 5: venue ────────────────────────────────────────────────────────
def venue_job(name, tf, mode):
    warm = "2024-06-01"
    b = get_bars(tf, start=warm)
    d = delta_perp_1h()
    d = d[d.index >= warm].resample(tf, closed="left", label="left").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum")).dropna(subset=["open", "close"])
    end = min(b.index.max(), d.index.max())
    b, d = b[b.index <= end], d[d.index <= end]
    mb = m_of(trades_for(b, name, mode), start=DELTA_FROM)
    md = m_of(trades_for(d, name, mode), start=DELTA_FROM)
    return {"strategy": name, "timeframe": tf, "mode": mode,
            "binance_trades": mb["total_trades"], "delta_trades": md["total_trades"],
            "binance_pnl": mb["net_pnl_usd"], "delta_pnl": md["net_pnl_usd"],
            "binance_sharpe": mb["sharpe"], "delta_sharpe": md["sharpe"]}


# ── test 6: walk-forward ─────────────────────────────────────────────────
def daily_pnl_job(name, tf, mode):
    # Mark-to-market, NOT exit-day booking — exit-day P&L hid open-trade
    # drawdowns and overstated the equal-weight basket's Sharpe 1.26 vs a
    # real 0.67 (2026-09-15).
    from backtesting.book_strategies.kaufman.run_btc_target import mtm_job
    return mtm_job(name, tf, mode)


def walk_forward(pnl: pd.DataFrame) -> dict:
    """pnl: daily $ P&L per combo (each sized on $10k). A portfolio of k combos
    splits the $10k k ways, so its daily P&L is the mean across members."""
    rng = np.random.default_rng(7)
    starts = pd.date_range(WF_START, pnl.index.max(), freq=f"{WF_HOLD_M}MS")
    picked, rand, allw = [], [[] for _ in range(100)], []
    log = []
    for s in starts:
        train = pnl[(pnl.index >= s - pd.DateOffset(months=WF_TRAIN_M)) & (pnl.index < s)]
        hold = pnl[(pnl.index >= s) & (pnl.index < s + pd.DateOffset(months=WF_HOLD_M))]
        if hold.empty:
            continue
        # only combos that existed (traded) during training are eligible
        active = train.columns[(train != 0).sum() >= 5]
        sh = (train[active].mean() / train[active].std().replace(0, np.nan)).dropna()
        top = sh.sort_values(ascending=False).head(WF_TOP).index
        picked.append(hold[top].mean(axis=1))
        allw.append(hold[active].mean(axis=1))
        for r in range(100):
            rand[r].append(hold[rng.choice(active, WF_TOP, replace=False)].mean(axis=1))
        log.append((s.date(), list(top[:3])))

    def stats(parts):
        p = pd.concat(parts)
        eq = CAPITAL + p.cumsum()
        yrs = (p.index[-1] - p.index[0]).days / 365.25
        return {"cagr": ((eq.iloc[-1] / CAPITAL) ** (1 / yrs) - 1) * 100,
                "dd": ((eq / eq.cummax()) - 1).min() * 100,
                "sharpe": p.mean() / p.std() * np.sqrt(365),
                "pnl": p.sum()}

    r_stats = [stats(x) for x in rand]
    sel = stats(picked)
    return {"selected": sel, "equal_all": stats(allw),
            "random_median": {k: float(np.median([r[k] for r in r_stats])) for k in sel},
            "random_beaten_pct": 100 * np.mean([sel["sharpe"] > r["sharpe"] for r in r_stats]),
            "log": log}


def main():
    t0 = time.perf_counter()
    for tf in TIMEFRAMES:
        get_bars(tf)
    screen = pd.read_csv(sorted(RESULTS_DIR.glob("btc_screen_*.csv"))[-1])

    print("=" * 120)
    print(f"  BTC ROBUSTNESS — {len(CANDIDATES)} candidates | split {SPLIT} | ${CAPITAL:,} 1% risk")
    print("=" * 120)

    with ProcessPoolExecutor(max_workers=6) as ex:
        cand = list(ex.map(candidate_job, *zip(*CANDIDATES)))
        venue = list(ex.map(venue_job, *zip(*CANDIDATES)))
        combos = [(r.strategy, r.timeframe, r["mode"]) for _, r in
                  screen[screen.timeframe.isin(WF_UNIVERSE_TFS)].iterrows()]
        daily = [x for x in ex.map(daily_pnl_job, *zip(*combos)) if x]

    cdf = pd.DataFrame(cand)
    vdf = pd.DataFrame(venue)

    # ── 1 + 3 + 4 ────────────────────────────────────────────────────────
    print("\n  [1] PARAMETER NEIGHBOURHOOD (30 perturbations +-40%)  [3] LOSING YEARS 2018-25  [4] 2x COST STRESS")
    cols = ["strategy", "timeframe", "mode", "oos_sharpe", "oos_cagr", "oos_dd", "oos_ratio",
            "neigh_oos_profitable_pct", "neigh_oos_sharpe_median", "neigh_oos_sharpe_p10",
            "default_oos_pctile", "losing_years", "stress_oos_pnl", "stress_oos_sharpe"]
    print(cdf[cols].to_string(index=False))

    print("\n  Year-by-year net P&L ($, default params):")
    years = list(range(2017, 2027))
    yt = pd.DataFrame([{**{"combo": f"{r['strategy']} {r['timeframe']} {r['mode']}"},
                        **{y: r["yearly"].get(y, 0) for y in years}} for r in cand])
    print(yt.to_string(index=False))

    # ── 2 ────────────────────────────────────────────────────────────────
    print("\n  [2] TIMEFRAME NEIGHBOURS — OOS Sharpe of same strategy+mode across timeframes")
    tf_rows = []
    for name, tf, mode in CANDIDATES:
        sub = screen[(screen.strategy == name) & (screen["mode"] == mode)].set_index("timeframe")
        tf_rows.append({"combo": f"{name} {tf} {mode}",
                        **{t: (round(sub.loc[t, "oos_sharpe"], 2) if t in sub.index else None)
                           for t in TIMEFRAMES}})
    print(pd.DataFrame(tf_rows).to_string(index=False))

    # ── 5 ────────────────────────────────────────────────────────────────
    print(f"\n  [5] VENUE CHECK — Binance vs Delta perp prices, trades entered {DELTA_FROM} -> now")
    print(vdf.to_string(index=False))

    # ── 6 ────────────────────────────────────────────────────────────────
    pnl = pd.concat({k: s for k, s in daily}, axis=1).fillna(0.0)
    pnl = pnl.reindex(pd.date_range(pnl.index.min(), pnl.index.max()), fill_value=0.0)
    wf = walk_forward(pnl)
    print(f"\n  [6] WALK-FORWARD SELECTION — universe {pnl.shape[1]} combos (4h+), "
          f"train {WF_TRAIN_M}m / hold {WF_HOLD_M}m / top {WF_TOP}, from {WF_START}")
    for label, k in (("Selected top-10", "selected"), ("Random 10 (median of 100)", "random_median"),
                     ("Equal-weight everything", "equal_all")):
        s = wf[k]
        print(f"    {label:<28} CAGR {s['cagr']:6.2f}%  MaxDD {s['dd']:7.2f}%  "
              f"Sharpe {s['sharpe']:5.2f}  P&L ${s['pnl']:>9,.0f}")
    print(f"    Selection beat {wf['random_beaten_pct']:.0f}% of random draws on Sharpe")
    print("    Picks per window (top 3):")
    for d0, top in wf["log"]:
        print(f"      {d0}  {', '.join(top)}")

    stamp = date.today()
    cdf.drop(columns="yearly").to_csv(RESULTS_DIR / f"btc_robust_candidates_{stamp}.csv", index=False)
    yt.to_csv(RESULTS_DIR / f"btc_robust_yearly_{stamp}.csv", index=False)
    vdf.to_csv(RESULTS_DIR / f"btc_robust_venue_{stamp}.csv", index=False)
    print(f"\n  CSVs in {RESULTS_DIR}")
    print(f"  Runtime: {time.perf_counter()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
