import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
XAUUSD EMA Gap Exhaustion Reversal — 2-min, 7 years
========================================================
Run: python forex/run_backtest_ema_gap_exhaustion.py
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

CONFIG = {**DEFAULT_CONFIG, "capital": 20_000, "risk_per_trade_pct": 0.01, "max_position_pct": 0.50,
          "z_threshold": 2.5, "min_streak": 2}


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
    daily = df.groupby("exit_date")["pnl"].sum().reset_index()
    daily["equity"] = capital + daily["pnl"].cumsum()
    daily["peak"] = daily["equity"].cummax()
    daily["drawdown_pct"] = (daily["equity"] - daily["peak"]) / daily["peak"] * 100

    dr = daily.set_index("exit_date")["pnl"] / capital
    sharpe = (dr.mean() / dr.std()) * np.sqrt(252) if dr.std() > 0 else 0.0
    max_dd = daily["drawdown_pct"].min()

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
        "final_capital":    round(final_cap, 2),
        "date_start":       str(date_start),
        "date_end":         str(date_end),
        "daily":            daily,
    }


def print_summary(metrics: dict, n_bars: int):
    line = "=" * 66
    print(f"\n{line}")
    print("  XAUUSD EMA GAP EXHAUSTION REVERSAL — 2-MIN, 7 YEARS")
    print(line)
    print(f"  Setup     : z > {CONFIG['z_threshold']} + {CONFIG['min_streak']}+ green above EMA9 "
          f"-> red signal candle (mirror for longs)")
    print(f"  Entry     : break of signal candle low (short) / high (long)")
    print(f"  SL        : signal candle high (short) / low (long)")
    print(f"  Exit      : SL hit OR z reverts to 0")
    print(f"  Bars      : {n_bars:,} 2-min bars")
    print(f"  Period    : {metrics.get('date_start')} -> {metrics.get('date_end')}")
    print(f"  Capital   : ${CONFIG['capital']:,.0f}  |  Risk/trade: {CONFIG['risk_per_trade_pct']*100:.0f}%")
    print(line)
    print(f"  Total Trades    : {metrics.get('total_trades', 0):,}  "
          f"({metrics.get('long_trades', 0)} long / {metrics.get('short_trades', 0)} short)")
    print(f"  Win Rate        : {metrics.get('win_rate_pct', 0):.1f}%")
    print(f"  Total PnL       : ${metrics.get('total_pnl', 0):,.2f}")
    print(f"  Net Return      : {metrics.get('net_return_pct', 0):.2f}%")
    print(f"  CAGR            : {metrics.get('cagr_pct', 0):.2f}%")
    print(f"  Sharpe Ratio    : {metrics.get('sharpe', 0):.3f}")
    print(f"  Max Drawdown    : {metrics.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Profit Factor   : {metrics.get('profit_factor', 0):.3f}")
    print(f"  Reverted exits  : {metrics.get('reverted', 0):,}")
    print(f"  Stops Hit       : {metrics.get('stops_hit', 0):,}")
    print(f"  Final Capital   : ${metrics.get('final_capital', 0):,.2f}")
    print(line)


def main():
    t_start = time.perf_counter()
    print("Loading cached 2-min data...")
    df_2min = pd.read_parquet(CACHE_FILE)
    print(f"Loaded {len(df_2min):,} bars ({time.perf_counter()-t_start:.1f}s)")

    trades = run_backtest(df_2min, CONFIG)
    if not trades:
        print("\n  No trades generated.")
        return

    trades_df = pd.DataFrame([t.to_dict() for t in trades])
    metrics = compute_metrics(trades_df, CONFIG["capital"])

    trades_csv = RESULTS_DIR / f"xauusd_gap_exhaustion_trades_{END_DATE}.csv"
    trades_df.to_csv(trades_csv, index=False)
    metrics["daily"].to_csv(RESULTS_DIR / f"xauusd_gap_exhaustion_daily_{END_DATE}.csv", index=False)

    print_summary(metrics, len(df_2min))
    print(f"\n  Trades CSV: {trades_csv}")
    print(f"  Total runtime: {time.perf_counter()-t_start:.1f}s\n")


if __name__ == "__main__":
    main()
