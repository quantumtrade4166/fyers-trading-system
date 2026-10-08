import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Kaufman Batch C — Adaptive Strategies: timeframe scan + parameter sweep
==========================================================================
Phase 1 (--scan) : every strategy at book-default parameters across
                   several timeframes — finds where each one actually works
Phase 2 (--sweep): parameter grid on a chosen timeframe

Run:
    python backtesting/book_strategies/kaufman/run_batch_adaptive.py --scan
    python backtesting/book_strategies/kaufman/run_batch_adaptive.py --sweep --tf 1h
"""

import argparse
import time
import itertools
from pathlib import Path
from datetime import date

import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data import get_bars, TEST_YEARS, START_DATE, END_DATE
from backtesting.book_strategies.kaufman.harness import backtest
from backtesting.book_strategies.kaufman.strategies_adaptive import REGISTRY

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CAPITAL = 20_000

BASE_CFG = {
    "capital":          CAPITAL,
    "risk_pct":         0.01,
    "max_position_pct": 0.50,
    "mode":             "flat",
    "entry_on":         "close",
    "default_stop_atr": 2.0,
    "cost_per_unit":    0.35,   # typical retail XAUUSD round trip
}

SCAN_TIMEFRAMES = ["1D", "4h", "1h", "15min"]

SWEEP_GRIDS = {
    "S088_KAMA": {
        "er_period":     [10, 20, 30],
        "slow":          [30, 50],
        "stop_atr_mult": [2.0, 3.0],
    },
    "S089_VIDYA": {
        "short_period":  [9, 14],
        "cmo_period":    [14, 20],
        "stop_atr_mult": [2.0, 3.0],
    },
    "S090_AdaptiveRSI": {
        "base_period":   [14, 21],
        "os_level":      [25, 30],
        "ob_level":      [70, 75],
        "stop_atr_mult": [2.0, 3.0],
    },
    "S092_AdaptiveBrkout": {
        "n_bars":        [5, 10],
        "range_power":   [0.5, 1.0],
        "stop_atr_mult": [2.0, 3.0],
    },
}

COLS = ["total_trades", "win_rate_pct", "profit_factor", "avg_rr",
        "sharpe", "cagr_pct", "max_drawdown_pct", "net_return_pct"]


def _row(name: str, extra: dict, m: dict) -> dict:
    return {"strategy": name, **extra,
            **{k: m.get(k, 0) for k in COLS},
            "stops": m.get("stops", 0), "signal_exits": m.get("signal_exits", 0)}


def scan():
    print("=" * 108)
    print(f"  KAUFMAN BATCH C — ADAPTIVE (S088-S092)  |  timeframe scan, book defaults")
    print(f"  XAUUSD  {START_DATE} -> {END_DATE} ({TEST_YEARS}y)  |  ${CAPITAL:,} capital, 1% risk")
    print("=" * 108)

    rows = []
    for tf in SCAN_TIMEFRAMES:
        t0 = time.perf_counter()
        df = get_bars(tf)
        print(f"\n  [{tf}] {len(df):,} bars  (load {time.perf_counter()-t0:.1f}s)")

        for name, fn in REGISTRY.items():
            t1 = time.perf_counter()
            # S092 has no book-implementable exit without VWAP — it needs the
            # chandelier trailing substitute or it degenerates into buy-and-hold
            cfg = {**BASE_CFG, "trailing": True} if name == "S092_AdaptiveBrkout" else BASE_CFG
            try:
                _, m = backtest(df.copy(), fn, cfg)
            except Exception as exc:
                print(f"    {name:<22} FAILED: {exc}")
                continue
            rows.append(_row(name, {"timeframe": tf}, m))
            print(f"    {name:<22} trades={m.get('total_trades',0):>6}  "
                  f"win%={m.get('win_rate_pct',0):>5.1f}  "
                  f"PF={m.get('profit_factor',0):>6.3f}  "
                  f"R:R=1:{m.get('avg_rr',0):>4.2f}  "
                  f"Sharpe={m.get('sharpe',0):>6.3f}  "
                  f"CAGR={m.get('cagr_pct',0):>7.2f}%  "
                  f"DD={m.get('max_drawdown_pct',0):>6.2f}%  "
                  f"({time.perf_counter()-t1:.1f}s)")

    res = pd.DataFrame(rows)
    out = RESULTS_DIR / f"kaufman_batchC_adaptive_scan_{END_DATE}.csv"
    res.to_csv(out, index=False)

    print("\n" + "=" * 108)
    print("  RANKED BY SHARPE (positive CAGR only)")
    print("=" * 108)
    good = res[res["cagr_pct"] > 0].sort_values("sharpe", ascending=False)
    if good.empty:
        print("  No configuration produced a positive CAGR.")
    else:
        print(good[["strategy", "timeframe"] + COLS].head(15).to_string(index=False))
    print(f"\n  CSV: {out}\n")


def sweep(tf: str):
    print("=" * 108)
    print(f"  KAUFMAN BATCH C — ADAPTIVE  |  parameter sweep @ {tf}")
    print(f"  XAUUSD  {START_DATE} -> {END_DATE} ({TEST_YEARS}y)  |  ${CAPITAL:,} capital, 1% risk")
    print("=" * 108)

    df = get_bars(tf)
    print(f"  {len(df):,} bars\n")

    rows = []
    for name, fn in REGISTRY.items():
        grid = SWEEP_GRIDS.get(name, {})
        keys = list(grid)
        combos = list(itertools.product(*[grid[k] for k in keys])) or [()]
        print(f"  {name} — {len(combos)} combos")

        for combo in combos:
            params = dict(zip(keys, combo))
            cfg = {**BASE_CFG, **params}
            if name == "S092_AdaptiveBrkout":
                cfg["trailing"] = True
            try:
                _, m = backtest(df.copy(), fn, cfg)
            except Exception as exc:
                print(f"    {params} FAILED: {exc}")
                continue
            rows.append(_row(name, {"timeframe": tf, **params}, m))

        best = [r for r in rows if r["strategy"] == name]
        if best:
            b = max(best, key=lambda r: r["sharpe"])
            print(f"    best: Sharpe={b['sharpe']:.3f} CAGR={b['cagr_pct']:.2f}% "
                  f"PF={b['profit_factor']:.3f} DD={b['max_drawdown_pct']:.2f}% "
                  f"| {[f'{k}={b[k]}' for k in SWEEP_GRIDS.get(name, {})]}")

    res = pd.DataFrame(rows)
    out = RESULTS_DIR / f"kaufman_batchC_adaptive_sweep_{tf}_{END_DATE}.csv"
    res.to_csv(out, index=False)

    print("\n" + "=" * 108)
    print("  TOP 20 BY SHARPE")
    print("=" * 108)
    show = [c for c in res.columns if c not in ("stops", "signal_exits")]
    print(res.sort_values("sharpe", ascending=False)[show].head(20).to_string(index=False))
    print(f"\n  CSV: {out}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--tf", default="1h")
    a = ap.parse_args()

    t0 = time.perf_counter()
    if a.sweep:
        sweep(a.tf)
    else:
        scan()
    print(f"  Total runtime: {time.perf_counter()-t0:.1f}s\n")
