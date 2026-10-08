import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
XAUUSD EMA Gap Exhaustion Reversal — z_threshold x min_streak sweep
========================================================================
Run: python forex/run_backtest_ema_gap_exhaustion_sweep.py
"""

import time
from pathlib import Path
from datetime import date, timedelta

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from forex.strategy_ema_gap_exhaustion import run_backtest, DEFAULT_CONFIG

TEST_YEARS = 7
END_DATE   = date.today()
START_DATE = END_DATE - timedelta(days=365 * TEST_YEARS)

CACHE_FILE = Path(__file__).parent / "_cache" / "xauusd_2min_7y.parquet"
RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BASE = {**DEFAULT_CONFIG, "capital": 20_000, "risk_per_trade_pct": 0.01, "max_position_pct": 0.50}

CONFIGS = {
    "z2.0_streak2": {**BASE, "z_threshold": 2.0, "min_streak": 2},
    "z2.0_streak3": {**BASE, "z_threshold": 2.0, "min_streak": 3},
    "z2.5_streak2": {**BASE, "z_threshold": 2.5, "min_streak": 2},
    "z2.5_streak3": {**BASE, "z_threshold": 2.5, "min_streak": 3},
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

    reason = df["exit_reason"].value_counts().to_dict()
    long_trades  = (df["direction"] == "long").sum()
    short_trades = (df["direction"] == "short").sum()

    return {
        "total_trades":     total,
        "long_trades":      long_trades,
        "short_trades":     short_trades,
        "win_rate_pct":     round(win_rate, 1),
        "total_pnl":        round(total_pnl, 2),
        "net_return_pct":   round(total_pnl / capital * 100, 2),
        "cagr_pct":         round(cagr, 2),
        "sharpe":           round(sharpe, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "profit_factor":    round(pf, 3),
        "reverted":         reason.get("reverted", 0),
        "stops_hit":        reason.get("stop", 0),
    }


def main():
    t_start = time.perf_counter()
    print("=" * 100)
    print("  XAUUSD EMA Gap Exhaustion Reversal — z_threshold x min_streak sweep (2-min, 7y)")
    print("=" * 100)

    df_2min = pd.read_parquet(CACHE_FILE)
    print(f"  Loaded {len(df_2min):,} 2-min bars\n")

    rows = []
    for name, cfg in CONFIGS.items():
        t0 = time.perf_counter()
        trades = run_backtest(df_2min, cfg)
        trades_df = pd.DataFrame([t.to_dict() for t in trades]) if trades else pd.DataFrame()
        metrics = compute_metrics(trades_df, cfg["capital"])
        metrics["config"] = name
        rows.append(metrics)

        if not trades_df.empty:
            trades_df.to_csv(RESULTS_DIR / f"xauusd_gap_exhaustion_{name}_{END_DATE}.csv", index=False)

        print(f"  [{time.perf_counter()-t0:6.1f}s] {name:<14} "
              f"trades={metrics.get('total_trades', 0):>5}  "
              f"win%={metrics.get('win_rate_pct', 0):>5.1f}  "
              f"PF={metrics.get('profit_factor', 0):>6.3f}  "
              f"Sharpe={metrics.get('sharpe', 0):>6.3f}  "
              f"CAGR={metrics.get('cagr_pct', 0):>7.2f}%  "
              f"MaxDD={metrics.get('max_drawdown_pct', 0):>6.2f}%")

    results_df = pd.DataFrame(rows).set_index("config")
    cols = ["total_trades", "long_trades", "short_trades", "win_rate_pct", "profit_factor",
            "sharpe", "cagr_pct", "max_drawdown_pct", "net_return_pct", "reverted", "stops_hit"]
    print("\n" + "=" * 100)
    print(results_df[cols].to_string())
    print("=" * 100)

    out_csv = RESULTS_DIR / f"xauusd_gap_exhaustion_sweep_{END_DATE}.csv"
    results_df.to_csv(out_csv)
    print(f"\n  Comparison CSV: {out_csv}")
    print(f"  Total runtime: {time.perf_counter()-t_start:.1f}s\n")


if __name__ == "__main__":
    main()
