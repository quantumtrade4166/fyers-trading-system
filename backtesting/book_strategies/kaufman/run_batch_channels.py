import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Kaufman Batch B — Channels & Bands: timeframe scan, sweep, cost check
========================================================================
    --scan            book defaults across timeframes
    --sweep --tf 4h   parameter grid on one timeframe
    --cost  --tf 4h   cost sensitivity on the survivors of --sweep

All money figures reported in absolute USD as well as percentages.
"""

import argparse
import time
import itertools
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data import get_bars, TEST_YEARS, START_DATE, END_DATE
from backtesting.book_strategies.kaufman.harness import backtest
from backtesting.book_strategies.kaufman.strategies_channels import REGISTRY, STRATEGY_CFG

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CAPITAL = 20_000
RETAIL_COST = 0.35          # USD/oz round trip — typical retail

BASE_CFG = {
    "capital": CAPITAL, "risk_pct": 0.01, "max_position_pct": 0.50,
    "mode": "flat", "entry_on": "close", "default_stop_atr": 2.0,
    "cost_per_unit": RETAIL_COST,
}

SCAN_TIMEFRAMES = ["1D", "4h", "1h"]

SWEEP_GRIDS = {
    "S019_LinRegBreak":  {"window": [20, 50], "width_mult": [1.5, 2.0]},
    "S020_LinRegMR":     {"window": [20, 50], "width_mult": [2.0, 2.5]},
    "S021_Donchian2010": {"entry_period": [20, 30], "exit_period": [10, 15], "stop_atr_mult": [2.0, 3.0]},
    "S022_Donchian4020": {"entry_period": [40, 55], "exit_period": [20], "stop_atr_mult": [2.0, 3.0]},
    "S023_KeltnerBreak": {"ema_period": [20, 40], "mult": [1.5, 2.0, 2.5]},
    "S023b_KeltnerMR":   {"ema_period": [20, 40], "mult": [2.0, 2.5]},
    "S024_BBBreak":      {"bb_period": [20, 40], "bb_std": [2.0, 2.5]},
    "S025_BBMeanRev":    {"bb_period": [20], "bb_std": [2.0, 2.5], "rsi_os": [30, 35]},
    "S026_ATRBands":     {"ma_period": [20, 40], "band_mult": [2.0, 2.5, 3.0]},
    "S027_PctBands":     {"ma_period": [20, 40], "band_pct": [0.01, 0.02, 0.03]},
    "S028_Fractal":      {"fractal_n": [2, 3, 5]},
}

HDR = (f"{'strategy':<20} {'trades':>7} {'win%':>6} {'PF':>6} {'R:R':>6} "
       f"{'Sharpe':>7} {'CAGR':>8} {'MaxDD':>8} {'net $':>10} {'final $':>10}")


def _line(name: str, m: dict) -> str:
    return (f"{name:<20} {m.get('total_trades',0):>7} {m.get('win_rate_pct',0):>6.1f} "
            f"{m.get('profit_factor',0):>6.3f} {m.get('avg_rr',0):>6.2f} "
            f"{m.get('sharpe',0):>7.3f} {m.get('cagr_pct',0):>7.2f}% "
            f"{m.get('max_drawdown_pct',0):>7.2f}% "
            f"{m.get('net_pnl_usd',0):>10,.0f} {m.get('final_equity_usd',0):>10,.0f}")


def _cfg_for(name: str, extra: dict = None) -> dict:
    return {**BASE_CFG, **STRATEGY_CFG.get(name, {}), **(extra or {})}


def scan():
    print("=" * 104)
    print("  KAUFMAN BATCH B — CHANNELS & BANDS (S019-S028)  |  timeframe scan, book defaults")
    print(f"  XAUUSD  {START_DATE} -> {END_DATE} ({TEST_YEARS}y)  |  ${CAPITAL:,} start, 1% risk, "
          f"cost ${RETAIL_COST:.2f}/oz (typical retail)")
    print("=" * 104)

    rows = []
    for tf in SCAN_TIMEFRAMES:
        df = get_bars(tf)
        print(f"\n  [{tf}] {len(df):,} bars")
        print("  " + HDR)
        print("  " + "-" * 100)
        for name, fn in REGISTRY.items():
            try:
                _, m = backtest(df.copy(), fn, _cfg_for(name))
            except Exception as exc:
                print(f"  {name:<20} FAILED: {exc}")
                continue
            rows.append({"strategy": name, "timeframe": tf, **m})
            print("  " + _line(name, m))

    res = pd.DataFrame(rows)
    out = RESULTS_DIR / f"kaufman_batchB_channels_scan_{END_DATE}.csv"
    res.to_csv(out, index=False)

    print("\n" + "=" * 104)
    print("  RANKED BY SHARPE (positive net PnL only, after cost)")
    print("=" * 104)
    good = res[res["net_pnl_usd"] > 0].sort_values("sharpe", ascending=False)
    cols = ["strategy", "timeframe", "total_trades", "win_rate_pct", "profit_factor",
            "avg_rr", "sharpe", "cagr_pct", "max_drawdown_pct", "net_pnl_usd", "final_equity_usd"]
    print(good[cols].head(15).to_string(index=False) if not good.empty
          else "  Nothing profitable after cost.")
    print(f"\n  CSV: {out}\n")


def sweep(tf: str):
    print("=" * 104)
    print(f"  KAUFMAN BATCH B — parameter sweep @ {tf}  |  cost ${RETAIL_COST:.2f}/oz")
    print("=" * 104)
    df = get_bars(tf)
    print(f"  {len(df):,} bars\n")

    rows = []
    for name, fn in REGISTRY.items():
        grid = SWEEP_GRIDS.get(name, {})
        keys = list(grid)
        combos = list(itertools.product(*[grid[k] for k in keys])) or [()]
        for combo in combos:
            params = dict(zip(keys, combo))
            try:
                _, m = backtest(df.copy(), fn, _cfg_for(name, params))
            except Exception as exc:
                print(f"    {name} {params} FAILED: {exc}")
                continue
            rows.append({"strategy": name, "timeframe": tf, **params, **m})

        sub = [r for r in rows if r["strategy"] == name]
        if sub:
            b = max(sub, key=lambda r: r["sharpe"])
            p = {k: b[k] for k in grid}
            print(f"  {name:<20} best Sharpe={b['sharpe']:>6.3f} CAGR={b['cagr_pct']:>6.2f}% "
                  f"PF={b['profit_factor']:>5.3f} DD={b['max_drawdown_pct']:>7.2f}% "
                  f"net=${b['net_pnl_usd']:>8,.0f}  {p}")

    res = pd.DataFrame(rows)
    out = RESULTS_DIR / f"kaufman_batchB_channels_sweep_{tf}_{END_DATE}.csv"
    res.to_csv(out, index=False)

    print("\n" + "=" * 104)
    print("  TOP 15 BY SHARPE")
    print("=" * 104)
    cols = ["strategy", "total_trades", "win_rate_pct", "profit_factor", "avg_rr",
            "sharpe", "cagr_pct", "max_drawdown_pct", "net_pnl_usd", "final_equity_usd"]
    print(res.sort_values("sharpe", ascending=False)[cols].head(15).to_string(index=False))
    print(f"\n  CSV: {out}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--tf", default="4h")
    a = ap.parse_args()

    t0 = time.perf_counter()
    if a.sweep:
        sweep(a.tf)
    else:
        scan()
    print(f"  Total runtime: {time.perf_counter()-t0:.1f}s\n")
