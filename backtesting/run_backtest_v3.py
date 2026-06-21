# ============================================================
# backtesting/run_backtest_v3.py
#
# BB Reversion Strategy — V3
#
# All V2 filters PLUS:
#   ✅ 1-hour 50 EMA trend filter
#      Short only if signal candle close < 50 EMA (1-hour)
#      Confirms we are shorting in a downtrend, not into strength
#
# Run: python backtesting/run_backtest_v3.py
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

from backtesting.data_loader            import DataLoader
from backtesting.strategy_bb_reversion  import run_symbol, DEFAULT_CONFIG

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG = {
    **DEFAULT_CONFIG,
    # Core
    "timeframe":             "15min",
    "bb_period":             20,
    "bb_std":                2.0,
    "min_outside_bars":      2,
    "capital":               1_000_000,
    "risk_per_trade":        2_000,
    "max_position_pct":      0.20,
    "exit_time":             "15:00",
    # V2 filters
    "direction":             "short",
    "max_signal_candle_pct": 0.008,
    "max_trades_per_day":    5,
    # V3 filter — 1-hour 50 EMA trend confirmation
    "ema_trend_filter":      True,
    "ema_trend_period":      50,
    "ema_trend_timeframe":   "1h",
}

RESULTS_DIR = Path(r"G:\Trading Backtesting\results")
RESULTS_DIR.mkdir(exist_ok=True)
RUN_TS      = datetime.now().strftime("%Y%m%d_%H%M")
TRADES_CSV  = RESULTS_DIR / f"bb_reversion_v3_trades_{RUN_TS}.csv"
SUMMARY_CSV = RESULTS_DIR / f"bb_reversion_v3_summary_{RUN_TS}.csv"

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s  %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("run_backtest_v3")


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(trades_df: pd.DataFrame, capital: float) -> dict:
    if trades_df.empty:
        return {}

    df = trades_df.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"]  = pd.to_datetime(df["exit_time"])

    total  = len(df)
    wins   = df[df["pnl"] > 0]
    losses = df[df["pnl"] < 0]

    total_pnl    = df["pnl"].sum()
    win_rate     = len(wins) / total * 100
    avg_winner   = wins["pnl"].mean()   if len(wins)   else 0
    avg_loser    = losses["pnl"].mean() if len(losses)  else 0
    gross_profit = wins["pnl"].sum()
    gross_loss   = abs(losses["pnl"].sum())
    pf           = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    date_start = df["entry_time"].min().date()
    date_end   = df["exit_time"].max().date()
    years      = max((date_end - date_start).days, 1) / 365.25
    final_cap  = capital + total_pnl
    cagr       = ((final_cap / capital) ** (1 / years) - 1) * 100

    df["exit_date"] = df["exit_time"].dt.date
    daily_pnl = df.groupby("exit_date")["pnl"].sum()
    if len(daily_pnl) > 1:
        dr     = daily_pnl / capital
        sharpe = (dr.mean() / dr.std()) * np.sqrt(252)
    else:
        sharpe = 0.0

    cum    = daily_pnl.cumsum() + capital
    max_dd = ((cum - cum.cummax()) / cum.cummax() * 100).min()

    reason = df["exit_reason"].value_counts().to_dict()

    return {
        "total_trades":     total,
        "win_rate_pct":     round(win_rate, 1),
        "total_pnl":        round(total_pnl, 0),
        "net_return_pct":   round(total_pnl / capital * 100, 2),
        "cagr_pct":         round(cagr, 2),
        "sharpe":           round(sharpe, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "avg_winner":       round(avg_winner, 0),
        "avg_loser":        round(avg_loser, 0),
        "profit_factor":    round(pf, 3),
        "avg_outside_bars": round(df["outside_bars"].mean(), 1) if "outside_bars" in df.columns else 0,
        "stops_hit":        reason.get("stop", 0) + reason.get("stop_same_bar", 0),
        "time_exits":       reason.get("time_exit", 0),
        "date_start":       str(date_start),
        "date_end":         str(date_end),
        "final_capital":    round(final_cap, 0),
    }


def print_summary(metrics: dict, sym_df: pd.DataFrame, n_symbols: int):
    line = "=" * 62
    print(f"\n{line}")
    print("  BB REVERSION V3 — SHORT + 1H 50 EMA TREND FILTER")
    print(line)
    print(f"  Filters   : short | candle≤0.8% | max5/day | ₹2k | 1h-EMA")
    print(f"  EMA       : {CONFIG['ema_trend_period']}-period on {CONFIG['ema_trend_timeframe']} bars")
    print(f"  Universe  : {n_symbols} symbols  |  BB: {CONFIG['bb_period']}p {CONFIG['bb_std']}std")
    print(f"  Period    : {metrics.get('date_start')} → {metrics.get('date_end')}")
    print(f"  Capital   : ₹{CONFIG['capital']:,.0f}  |  Risk/trade: ₹{CONFIG['risk_per_trade']:,}")
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
    print(f"  Stops Hit       : {metrics.get('stops_hit', 0):,}")
    print(f"  Time Exits      : {metrics.get('time_exits', 0):,}")
    print(f"  Final Capital   : ₹{metrics.get('final_capital', 0):,.0f}")
    print(line)

    if not sym_df.empty:
        top10 = sym_df[sym_df["total_trades"] > 0].nlargest(10, "total_pnl")
        print("\n  Top 10 Symbols by PnL:")
        print(f"  {'Symbol':<28} {'Trades':>6} {'Win%':>6} {'PnL (₹)':>10}")
        print(f"  {'-'*28} {'-'*6} {'-'*6} {'-'*10}")
        for _, r in top10.iterrows():
            print(f"  {r['symbol']:<28} {int(r['total_trades']):>6} "
                  f"{r['win_rate_pct']:>5.1f}% {r['total_pnl']:>10,.0f}")

        bot5 = sym_df[sym_df["total_trades"] > 0].nsmallest(5, "total_pnl")
        print("\n  Bottom 5 Symbols by PnL:")
        print(f"  {'Symbol':<28} {'Trades':>6} {'Win%':>6} {'PnL (₹)':>10}")
        print(f"  {'-'*28} {'-'*6} {'-'*6} {'-'*10}")
        for _, r in bot5.iterrows():
            print(f"  {r['symbol']:<28} {int(r['total_trades']):>6} "
                  f"{r['win_rate_pct']:>5.1f}% {r['total_pnl']:>10,.0f}")

    print(f"\n  Trades CSV : {TRADES_CSV}")
    print(f"  Summary CSV: {SUMMARY_CSV}")
    print(f"{line}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t_start = time.perf_counter()
    print("=" * 62)
    print("  BB Reversion V3 — Short + 1H 50 EMA Filter")
    print("=" * 62)

    loader  = DataLoader()
    symbols = loader.available_symbols()
    max_tpd = CONFIG["max_trades_per_day"]

    print(f"\n  Universe  : {len(symbols)} symbols")
    print(f"  Direction : SHORT only")
    print(f"  EMA filter: {CONFIG['ema_trend_period']}-period on {CONFIG['ema_trend_timeframe']} "
          f"— signal close must be below EMA")
    print(f"  Candle    : (H-L)/close ≤ {CONFIG['max_signal_candle_pct']*100:.1f}%")
    print(f"  Max/day   : {max_tpd} trades  (₹{CONFIG['risk_per_trade']:,} each)\n")

    all_trades = []
    sym_rows   = []
    failed     = []

    for i, symbol in enumerate(symbols, 1):
        try:
            df_5min = loader.load(symbol)
            trades  = run_symbol(symbol, df_5min, CONFIG)

            n = len(trades)
            if n > 0:
                tdf  = pd.DataFrame([t.to_dict() for t in trades])
                wins = (tdf["pnl"] > 0).sum()
                sym_rows.append({
                    "symbol":       symbol,
                    "total_trades": n,
                    "win_rate_pct": round(wins / n * 100, 1),
                    "total_pnl":    round(tdf["pnl"].sum(), 0),
                })
                all_trades.extend(trades)
            else:
                sym_rows.append({"symbol": symbol, "total_trades": 0,
                                  "win_rate_pct": 0.0, "total_pnl": 0.0})

            if i % 20 == 0 or i == len(symbols):
                print(f"  [{i:>3}/{len(symbols)}] {len(all_trades):>5} raw signals  "
                      f"({time.perf_counter()-t_start:.1f}s)")

        except Exception as exc:
            logger.warning(f"SKIP {symbol}: {exc}")
            failed.append(symbol)

    if not all_trades:
        print("\n  No trades generated.")
        return

    # ── Apply daily trade cap ─────────────────────────────────────────────────
    print(f"\n  Applying {max_tpd}-trades/day cap...")
    raw_df = pd.DataFrame([t.to_dict() for t in all_trades])
    raw_df["entry_time"] = pd.to_datetime(raw_df["entry_time"])
    raw_df["entry_date"] = raw_df["entry_time"].dt.date

    capped_df = (
        raw_df
        .sort_values("entry_time")
        .groupby("entry_date", group_keys=False)
        .head(max_tpd)
        .reset_index(drop=True)
    )

    # Per-symbol summary on capped trades
    sym_capped = (
        capped_df.groupby("symbol")
        .apply(lambda g: pd.Series({
            "total_trades": len(g),
            "win_rate_pct": round((g["pnl"] > 0).mean() * 100, 1),
            "total_pnl":    round(g["pnl"].sum(), 0),
        }), include_groups=False)
        .reset_index()
    )

    daily_counts = capped_df.groupby("entry_date").size()
    print(f"\n  After cap:")
    print(f"    Total trades : {len(capped_df):,}  (was {len(raw_df):,} before cap)")
    print(f"    Trading days : {len(daily_counts)}")
    print(f"    Avg/day      : {daily_counts.mean():.1f}")
    print(f"    Max/day      : {daily_counts.max()}")

    metrics = compute_metrics(capped_df, CONFIG["capital"])

    capped_df.drop(columns=["entry_date"], errors="ignore").to_csv(TRADES_CSV,  index=False)
    sym_capped.to_csv(SUMMARY_CSV, index=False)

    print_summary(metrics, sym_capped, len(symbols) - len(failed))

    if failed:
        print(f"  Skipped: {failed}")
    print(f"  Runtime: {time.perf_counter()-t_start:.1f}s\n")


if __name__ == "__main__":
    main()
