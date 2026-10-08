import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
XAUUSD EMA Crossover — Multi-Config Comparison
==================================================
Loads 5-min data ONCE (cached to disk after first load), then runs
several tuning variants against it to see which levers actually help.

Run: python forex/run_backtest_ema_compare.py
"""

import time
from pathlib import Path
from datetime import date, timedelta

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from forex.xauusd_data_loader import load_xauusd
from forex.strategy_ema_crossover import run_backtest, DEFAULT_CONFIG

TEST_YEARS = 3
END_DATE   = date.today()
START_DATE = END_DATE - timedelta(days=365 * TEST_YEARS)

CACHE_DIR  = Path(__file__).parent / "_cache"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_FILE = CACHE_DIR / f"xauusd_5min_{START_DATE}_{END_DATE}.parquet"

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BASE = {
    **DEFAULT_CONFIG,
    "capital":        100_000,
    "risk_per_trade": 1_000,
    "max_position_pct": 0.50,
}

CONFIGS = {
    "Baseline   9/21  no filter":       {**BASE, "fast_ema": 9,  "slow_ema": 21},
    "V_wide     20/50 no filter":       {**BASE, "fast_ema": 20, "slow_ema": 50},
    "V_fib      13/34 no filter":       {**BASE, "fast_ema": 13, "slow_ema": 34},
    "V_trend100 9/21  +trend100":       {**BASE, "fast_ema": 9,  "slow_ema": 21, "trend_ema": 100},
    "V_trend200 9/21  +trend200":       {**BASE, "fast_ema": 9,  "slow_ema": 21, "trend_ema": 200},
    "V_minsl    9/21  +minSL$2":        {**BASE, "fast_ema": 9,  "slow_ema": 21, "min_sl_distance": 2.0},
    "V_combo    9/21  +trend100+minSL": {**BASE, "fast_ema": 9,  "slow_ema": 21, "trend_ema": 100, "min_sl_distance": 2.0},
}


def compute_metrics(trades_df: pd.DataFrame, capital: float) -> dict:
    if trades_df.empty:
        return {"total_trades": 0}

    df = trades_df.copy()
    df["signal_time"] = pd.to_datetime(df["signal_time"])
    df["exit_time"]   = pd.to_datetime(df["exit_time"])

    total  = len(df)
    wins   = df[df["pnl"] > 0]
    losses = df[df["pnl"] < 0]

    total_pnl    = df["pnl"].sum()
    win_rate     = len(wins) / total * 100
    gross_profit = wins["pnl"].sum()
    gross_loss   = abs(losses["pnl"].sum())
    pf           = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    date_start = df["signal_time"].min().date()
    date_end   = df["exit_time"].max().date()
    years      = max((date_end - date_start).days, 1) / 365.25
    final_cap  = capital + total_pnl
    cagr       = ((final_cap / capital) ** (1 / years) - 1) * 100 if final_cap > 0 else -100.0

    df["exit_date"] = df["exit_time"].dt.date
    daily_pnl = df.groupby("exit_date")["pnl"].sum()
    if len(daily_pnl) > 1 and daily_pnl.std() > 0:
        sharpe = (daily_pnl.mean() / daily_pnl.std()) * np.sqrt(252)
    else:
        sharpe = 0.0

    cum    = daily_pnl.cumsum() + capital
    max_dd = ((cum - cum.cummax()) / cum.cummax() * 100).min()

    return {
        "total_trades":     total,
        "win_rate_pct":     round(win_rate, 1),
        "total_pnl":        round(total_pnl, 2),
        "net_return_pct":   round(total_pnl / capital * 100, 2),
        "cagr_pct":         round(cagr, 2),
        "sharpe":           round(sharpe, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "profit_factor":    round(pf, 3),
    }


def main():
    t_start = time.perf_counter()
    print("=" * 78)
    print("  XAUUSD EMA Crossover — Multi-Config Comparison")
    print("=" * 78)
    print(f"  Window: {START_DATE} -> {END_DATE} ({TEST_YEARS} years)")

    if CACHE_FILE.exists():
        print(f"  Loading cached 5-min data: {CACHE_FILE}")
        df_5min = pd.read_parquet(CACHE_FILE)
    else:
        df_5min = load_xauusd(str(START_DATE), str(END_DATE), timeframe="5min")
        df_5min.to_parquet(CACHE_FILE)
        print(f"  Cached to: {CACHE_FILE}")

    print(f"  {len(df_5min):,} 5-min bars ({time.perf_counter()-t_start:.1f}s)\n")

    rows = []
    for name, cfg in CONFIGS.items():
        t0 = time.perf_counter()
        trades = run_backtest(df_5min, cfg)
        trades_df = pd.DataFrame([t.to_dict() for t in trades]) if trades else pd.DataFrame()
        metrics = compute_metrics(trades_df, cfg["capital"])
        metrics["config"] = name
        rows.append(metrics)
        print(f"  [{time.perf_counter()-t0:5.1f}s] {name:<38} "
              f"trades={metrics.get('total_trades', 0):>5}  "
              f"PF={metrics.get('profit_factor', 0):>6.3f}  "
              f"Sharpe={metrics.get('sharpe', 0):>6.3f}  "
              f"CAGR={metrics.get('cagr_pct', 0):>7.2f}%  "
              f"MaxDD={metrics.get('max_drawdown_pct', 0):>6.2f}%")

    results_df = pd.DataFrame(rows).set_index("config")
    cols = ["total_trades", "win_rate_pct", "profit_factor", "sharpe",
            "cagr_pct", "max_drawdown_pct", "net_return_pct"]
    print("\n" + "=" * 78)
    print(results_df[cols].to_string())
    print("=" * 78)

    out_csv = RESULTS_DIR / f"xauusd_ema_compare_{END_DATE}.csv"
    results_df.to_csv(out_csv)
    print(f"\n  Comparison CSV: {out_csv}")
    print(f"  Total runtime: {time.perf_counter()-t_start:.1f}s\n")


if __name__ == "__main__":
    main()
