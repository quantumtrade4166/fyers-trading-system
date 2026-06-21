# ============================================================
# backtesting/run_backtest.py
#
# Main backtest runner — BB Reversion Strategy
# Runs across all 182 Nifty F&O stocks, outputs:
#   1. Trade-by-trade CSV  →  G:\Trading Backtesting\results\
#   2. Per-symbol summary  →  G:\Trading Backtesting\results\
#   3. Console summary table
#
# Run: python backtesting/run_backtest.py
# ============================================================

import sys
import logging
import time
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.data_loader        import DataLoader
from backtesting.strategy_bb_reversion import run_symbol, DEFAULT_CONFIG

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG = {
    **DEFAULT_CONFIG,
    "timeframe":        "15min",
    "bb_period":        20,
    "bb_std":           2.0,
    "min_outside_bars": 2,
    "capital":          1_000_000,   # ₹10,00,000
    "risk_per_trade":   10_000,      # ₹10,000 per trade (1%)
    "max_position_pct": 0.20,        # max 20% of capital per trade
    "exit_time":        "15:00",     # hard exit at 3:00 PM bar close
}

RESULTS_DIR = Path(r"G:\Trading Backtesting\results")
RESULTS_DIR.mkdir(exist_ok=True)

RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M")
TRADES_CSV    = RESULTS_DIR / f"bb_reversion_trades_{RUN_TIMESTAMP}.csv"
SUMMARY_CSV   = RESULTS_DIR / f"bb_reversion_summary_{RUN_TIMESTAMP}.csv"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level   = logging.WARNING,      # suppress per-bar DEBUG noise
    format  = "%(levelname)s  %(name)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("run_backtest")


# ── Performance metrics ───────────────────────────────────────────────────────

def compute_metrics(trades_df: pd.DataFrame, capital: float) -> dict:
    """
    Compute portfolio-level performance metrics from a trades DataFrame.

    Parameters
    ----------
    trades_df : pd.DataFrame  — all trades across all symbols
    capital   : float         — starting capital in ₹

    Returns
    -------
    dict with keys:
      total_trades, win_rate, total_pnl, net_return_pct,
      cagr, sharpe, max_drawdown_pct, avg_winner, avg_loser,
      profit_factor, avg_outside_bars
    """
    if trades_df.empty:
        return {}

    df = trades_df.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"]  = pd.to_datetime(df["exit_time"])

    total_trades = len(df)
    winners      = df[df["pnl"] > 0]
    losers       = df[df["pnl"] < 0]
    win_rate     = len(winners) / total_trades * 100

    total_pnl     = df["pnl"].sum()
    net_return    = total_pnl / capital * 100

    avg_winner = winners["pnl"].mean() if len(winners) else 0
    avg_loser  = losers["pnl"].mean()  if len(losers)  else 0

    gross_profit = winners["pnl"].sum()
    gross_loss   = abs(losers["pnl"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # ── CAGR ─────────────────────────────────────────────────────────────────
    date_start = df["entry_time"].min().date()
    date_end   = df["exit_time"].max().date()
    days       = max((date_end - date_start).days, 1)
    years      = days / 365.25
    final_cap  = capital + total_pnl
    cagr = ((final_cap / capital) ** (1 / years) - 1) * 100 if years > 0 else 0

    # ── Daily PnL series for Sharpe + Max Drawdown ───────────────────────────
    df["exit_date"] = df["exit_time"].dt.date
    daily_pnl = df.groupby("exit_date")["pnl"].sum()

    # Sharpe (annualised, using daily returns, 252 trading days)
    if len(daily_pnl) > 1:
        daily_ret = daily_pnl / capital
        sharpe    = (daily_ret.mean() / daily_ret.std()) * np.sqrt(252)
    else:
        sharpe = 0.0

    # Max Drawdown
    cumulative = daily_pnl.cumsum() + capital
    rolling_max = cumulative.cummax()
    drawdown    = (cumulative - rolling_max) / rolling_max * 100
    max_dd      = drawdown.min()

    avg_outside = df["outside_bars"].mean() if "outside_bars" in df.columns else 0

    return {
        "total_trades":    total_trades,
        "win_rate_pct":    round(win_rate, 1),
        "total_pnl":       round(total_pnl, 0),
        "net_return_pct":  round(net_return, 2),
        "cagr_pct":        round(cagr, 2),
        "sharpe":          round(sharpe, 3),
        "max_drawdown_pct":round(max_dd, 2),
        "avg_winner":      round(avg_winner, 0),
        "avg_loser":       round(avg_loser, 0),
        "profit_factor":   round(profit_factor, 3),
        "avg_outside_bars":round(avg_outside, 1),
        "date_start":      str(date_start),
        "date_end":        str(date_end),
        "final_capital":   round(final_cap, 0),
    }


def print_summary(metrics: dict, symbol_summary: pd.DataFrame):
    """Print a formatted summary to the console."""
    line = "=" * 60

    print(f"\n{line}")
    print("  BB REVERSION BACKTEST — RESULTS SUMMARY")
    print(line)
    print(f"  Strategy  : Bollinger Band Mean Reversion")
    print(f"  Timeframe : 15-min bars")
    print(f"  BB params : {CONFIG['bb_period']} period, {CONFIG['bb_std']} std dev")
    print(f"  Universe  : {len(symbol_summary)} symbols")
    print(f"  Period    : {metrics.get('date_start')} → {metrics.get('date_end')}")
    print(f"  Capital   : ₹{CONFIG['capital']:,.0f}")
    print(f"  Risk/trade: ₹{CONFIG['risk_per_trade']:,.0f}")
    print(line)

    print(f"  Total Trades    : {metrics.get('total_trades', 0):,}")
    print(f"  Win Rate        : {metrics.get('win_rate_pct', 0):.1f}%")
    print(f"  Total PnL       : ₹{metrics.get('total_pnl', 0):,.0f}")
    print(f"  Net Return      : {metrics.get('net_return_pct', 0):.2f}%")
    print(f"  CAGR            : {metrics.get('cagr_pct', 0):.2f}%")
    print(f"  Sharpe Ratio    : {metrics.get('sharpe', 0):.3f}")
    print(f"  Max Drawdown    : {metrics.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Avg Winner      : ₹{metrics.get('avg_winner', 0):,.0f}")
    print(f"  Avg Loser       : ₹{metrics.get('avg_loser', 0):,.0f}")
    print(f"  Profit Factor   : {metrics.get('profit_factor', 0):.3f}")
    print(f"  Avg Outside Bars: {metrics.get('avg_outside_bars', 0):.1f}")
    print(f"  Final Capital   : ₹{metrics.get('final_capital', 0):,.0f}")
    print(line)

    # Top 10 symbols by PnL
    if not symbol_summary.empty:
        top10 = symbol_summary.nlargest(10, "total_pnl")
        print("\n  Top 10 Symbols by PnL:")
        print(f"  {'Symbol':<30} {'Trades':>6} {'Win%':>6} {'PnL (₹)':>10}")
        print(f"  {'-'*30} {'-'*6} {'-'*6} {'-'*10}")
        for _, row in top10.iterrows():
            print(
                f"  {row['symbol']:<30} "
                f"{int(row['total_trades']):>6} "
                f"{row['win_rate_pct']:>5.1f}% "
                f"{row['total_pnl']:>10,.0f}"
            )

        worst5 = symbol_summary.nsmallest(5, "total_pnl")
        print("\n  Bottom 5 Symbols by PnL:")
        print(f"  {'Symbol':<30} {'Trades':>6} {'Win%':>6} {'PnL (₹)':>10}")
        print(f"  {'-'*30} {'-'*6} {'-'*6} {'-'*10}")
        for _, row in worst5.iterrows():
            print(
                f"  {row['symbol']:<30} "
                f"{int(row['total_trades']):>6} "
                f"{row['win_rate_pct']:>5.1f}% "
                f"{row['total_pnl']:>10,.0f}"
            )

    print(f"\n  Trades CSV : {TRADES_CSV}")
    print(f"  Summary CSV: {SUMMARY_CSV}")
    print(f"{line}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t_start = time.perf_counter()
    print("=" * 60)
    print("  BB Reversion Backtest — Starting")
    print("=" * 60)

    # ── Load symbol list ──────────────────────────────────────────────────────
    loader  = DataLoader()
    symbols = loader.available_symbols()
    print(f"\n  Universe  : {len(symbols)} symbols")
    print(f"  Risk/trade: ₹{CONFIG['risk_per_trade']:,}")
    print(f"  Capital   : ₹{CONFIG['capital']:,}")
    print(f"  BB params : {CONFIG['bb_period']}-period, {CONFIG['bb_std']} std")
    print(f"  Min bars  : {CONFIG['min_outside_bars']} consecutive outside\n")

    # ── Run strategy on each symbol ───────────────────────────────────────────
    all_trades   = []
    sym_rows     = []
    failed       = []

    for i, symbol in enumerate(symbols, 1):
        try:
            df_5min = loader.load(symbol)
            trades  = run_symbol(symbol, df_5min, CONFIG)

            n = len(trades)
            if n > 0:
                trades_df = pd.DataFrame([t.to_dict() for t in trades])
                wins      = (trades_df["pnl"] > 0).sum()
                sym_pnl   = trades_df["pnl"].sum()
                sym_rows.append({
                    "symbol":      symbol,
                    "total_trades": n,
                    "win_rate_pct": round(wins / n * 100, 1),
                    "total_pnl":   round(sym_pnl, 0),
                })
                all_trades.extend(trades)
            else:
                sym_rows.append({
                    "symbol": symbol,
                    "total_trades": 0,
                    "win_rate_pct": 0.0,
                    "total_pnl": 0.0,
                })

            # Progress every 20 symbols
            if i % 20 == 0 or i == len(symbols):
                elapsed = time.perf_counter() - t_start
                trades_so_far = len(all_trades)
                print(
                    f"  [{i:>3}/{len(symbols)}] "
                    f"{trades_so_far:>5} trades so far  "
                    f"({elapsed:.1f}s elapsed)"
                )

        except Exception as exc:
            logger.warning(f"  SKIP {symbol}: {exc}")
            failed.append(symbol)

    # ── Build results DataFrames ──────────────────────────────────────────────
    if not all_trades:
        print("\n  No trades generated. Check strategy parameters.")
        return

    trades_df  = pd.DataFrame([t.to_dict() for t in all_trades])
    sym_df     = pd.DataFrame(sym_rows).sort_values("total_pnl", ascending=False)

    # ── Compute portfolio metrics ─────────────────────────────────────────────
    metrics = compute_metrics(trades_df, CONFIG["capital"])

    # ── Save to CSV ───────────────────────────────────────────────────────────
    trades_df.to_csv(TRADES_CSV,  index=False)
    sym_df.to_csv(SUMMARY_CSV,    index=False)

    # ── Print summary ─────────────────────────────────────────────────────────
    print_summary(metrics, sym_df)

    if failed:
        print(f"  Skipped {len(failed)} symbols: {failed[:5]}{'...' if len(failed)>5 else ''}")

    total_time = time.perf_counter() - t_start
    print(f"  Total runtime: {total_time:.1f}s\n")


if __name__ == "__main__":
    main()
