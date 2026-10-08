import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
XAUUSD 9/21 EMA Crossover — 5-min Backtest
=============================================
Entry  : close of the bar where EMA9 crosses EMA21
SL     : low (long) / high (short) of the crossover bar
Exit   : stop loss OR opposite crossover (always-in-market reversal system)

Run: python forex/run_backtest_ema_crossover.py
"""

import time
from pathlib import Path
from datetime import date, timedelta

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from forex.xauusd_data_loader import load_xauusd
from forex.strategy_ema_crossover import run_backtest, DEFAULT_CONFIG

CONFIG = {**DEFAULT_CONFIG}

TEST_YEARS = 3
END_DATE   = date.today()
START_DATE = END_DATE - timedelta(days=365 * TEST_YEARS)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
TRADES_CSV  = RESULTS_DIR / f"xauusd_ema_crossover_trades_{END_DATE}.csv"


def compute_metrics(trades_df: pd.DataFrame, capital: float) -> dict:
    if trades_df.empty:
        return {}

    df = trades_df.copy()
    df["signal_time"] = pd.to_datetime(df["signal_time"])
    df["exit_time"]   = pd.to_datetime(df["exit_time"])

    total  = len(df)
    wins   = df[df["pnl"] > 0]
    losses = df[df["pnl"] < 0]

    total_pnl    = df["pnl"].sum()
    win_rate     = len(wins) / total * 100
    avg_winner   = wins["pnl"].mean()   if len(wins)   else 0
    avg_loser    = losses["pnl"].mean() if len(losses) else 0
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

    if len(daily_pnl) > 1:
        dr     = daily_pnl / capital
        sharpe = (dr.mean() / dr.std()) * np.sqrt(252) if dr.std() > 0 else 0.0
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
        "avg_winner":       round(avg_winner, 2),
        "avg_loser":        round(avg_loser, 2),
        "profit_factor":    round(pf, 3),
        "stops_hit":        reason.get("stop", 0),
        "reversals":        reason.get("reverse", 0),
        "date_start":       str(date_start),
        "date_end":         str(date_end),
        "final_capital":    round(final_cap, 2),
    }


def print_summary(metrics: dict, n_bars: int):
    line = "=" * 62
    print(f"\n{line}")
    print("  XAUUSD 9/21 EMA CROSSOVER — 5-MIN BACKTEST")
    print(line)
    print(f"  Entry     : close of EMA9/EMA21 crossover bar")
    print(f"  SL        : low (long) / high (short) of crossover bar")
    print(f"  Exit      : SL hit OR opposite crossover (reverse)")
    print(f"  Bars      : {n_bars:,} 5-min bars")
    print(f"  Period    : {metrics.get('date_start')} -> {metrics.get('date_end')}")
    print(f"  Capital   : ${CONFIG['capital']:,.0f}  |  Risk/trade: ${CONFIG['risk_per_trade']:,.0f}")
    print(line)
    print(f"  Total Trades    : {metrics.get('total_trades', 0):,}  "
          f"({metrics.get('long_trades', 0)} long / {metrics.get('short_trades', 0)} short)")
    print(f"  Win Rate        : {metrics.get('win_rate_pct', 0):.1f}%")
    print(f"  Total PnL       : ${metrics.get('total_pnl', 0):,.2f}")
    print(f"  Net Return      : {metrics.get('net_return_pct', 0):.2f}%")
    print(f"  CAGR            : {metrics.get('cagr_pct', 0):.2f}%")
    print(f"  Sharpe Ratio    : {metrics.get('sharpe', 0):.3f}")
    print(f"  Max Drawdown    : {metrics.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Avg Winner      : ${metrics.get('avg_winner', 0):,.2f}")
    print(f"  Avg Loser       : ${metrics.get('avg_loser', 0):,.2f}")
    print(f"  Profit Factor   : {metrics.get('profit_factor', 0):.3f}")
    print(f"  Stops Hit       : {metrics.get('stops_hit', 0):,}")
    print(f"  Reversals       : {metrics.get('reversals', 0):,}")
    print(f"  Final Capital   : ${metrics.get('final_capital', 0):,.2f}")
    print(line)
    print(f"\n  Trades CSV : {TRADES_CSV}")
    print(f"{line}\n")


def main():
    t_start = time.perf_counter()
    print("=" * 62)
    print("  XAUUSD 9/21 EMA Crossover — Loading & resampling data")
    print("=" * 62)
    print(f"  Window: {START_DATE} -> {END_DATE} ({TEST_YEARS} years)")

    df_5min = load_xauusd(str(START_DATE), str(END_DATE), timeframe="5min")
    print(f"  Loaded {len(df_5min):,} 5-min bars ({time.perf_counter()-t_start:.1f}s)")

    trades = run_backtest(df_5min, CONFIG)
    if not trades:
        print("\n  No trades generated.")
        return

    trades_df = pd.DataFrame([t.to_dict() for t in trades])
    metrics = compute_metrics(trades_df, CONFIG["capital"])

    trades_df.to_csv(TRADES_CSV, index=False)
    print_summary(metrics, len(df_5min))
    print(f"  Total runtime: {time.perf_counter()-t_start:.1f}s\n")


if __name__ == "__main__":
    main()
