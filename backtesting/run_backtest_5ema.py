# ============================================================
# backtesting/run_backtest_5ema.py
#
# 5 EMA 5-Min Short — Gap-Up Reversal Strategy  (V2)
#
# Rules:
#   ✅ Gap-up ≥ 0.8% at open (today vs yesterday close)
#   ✅ Bar 1 (09:15): green AND close > 5 EMA
#   ✅ Bar 2 (09:20): green AND close > 5 EMA
#   ✅ Bar 3+: close > 5 EMA (any colour, continuous streak)
#   ✅ Signal candle range ≤ 0.5%  → entry below low, SL above high
#   ✅ No entries at or after 10:00 AM
#   ✅ One trade per stock per day
#   ✅ Max 5 trades/day across all stocks (₹2,000 risk each)
#
# Run: python backtesting/run_backtest_5ema.py
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

from backtesting.data_loader       import DataLoader
from backtesting.strategy_5ema_short import run_symbol, DEFAULT_CONFIG

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG = {
    **DEFAULT_CONFIG,
    "ema_period":             5,
    "gap_up_pct":             0.015,    # 1.5% gap-up required  (V3)
    "max_trigger_candle_pct": 0.005,    # signal candle range ≤ 0.5%  (V4)
    "signal_vol_ratio":       0.70,     # signal vol < 70% of bar-1 vol  (V3)
    "capital":                1_000_000,
    "risk_per_trade":         2_000,    # ₹2,000 per trade
    "max_position_pct":       0.20,
    "exit_time":              "15:00",
    "no_entry_after":         "10:00",
    "max_trades_per_day":     5,
}

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
RUN_TS      = datetime.now().strftime("%Y%m%d_%H%M")
TRADES_CSV  = RESULTS_DIR / f"5ema_short_trades_{RUN_TS}.csv"
SUMMARY_CSV = RESULTS_DIR / f"5ema_short_summary_{RUN_TS}.csv"

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("run_5ema")


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
    daily_counts = df.groupby("exit_date").size()
    avg_green = df["green_bars"].mean() if "green_bars" in df.columns else 0

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
        "avg_green_bars":   round(avg_green, 1),
        "stops_hit":        reason.get("stop", 0) + reason.get("stop_same_bar", 0),
        "time_exits":       reason.get("time_exit", 0),
        "avg_per_day":      round(daily_counts.mean(), 1),
        "max_per_day":      int(daily_counts.max()),
        "trading_days":     len(daily_counts),
        "date_start":       str(date_start),
        "date_end":         str(date_end),
        "final_capital":    round(final_cap, 0),
    }


# ── Print summary ──────────────────────────────────────────────────────────────

def print_summary(metrics: dict, sym_df: pd.DataFrame, n_symbols: int):
    line = "=" * 62
    print(f"\n{line}")
    print("  5 EMA SHORT — GAP-UP REVERSAL STRATEGY")
    print(line)
    print(f"  Setup     : gap-up ≥ {CONFIG['gap_up_pct']*100:.1f}% | "
          f"bar1+bar2 green+above {CONFIG['ema_period']} EMA | "
          f"bar3+ above EMA | signal range ≤ {CONFIG['max_trigger_candle_pct']*100:.1f}%")
    print(f"  Entry     : short below signal candle low")
    print(f"  SL        : signal candle high")
    print(f"  Cutoff    : no entries at/after {CONFIG['no_entry_after']} | "
          f"exit at {CONFIG['exit_time']}")
    print(f"  Universe  : {n_symbols} symbols (5-min bars)")
    print(f"  Period    : {metrics.get('date_start')} → {metrics.get('date_end')}")
    print(f"  Capital   : ₹{CONFIG['capital']:,.0f}  |  "
          f"Risk/trade: ₹{CONFIG['risk_per_trade']:,}  |  "
          f"Max/day: {CONFIG['max_trades_per_day']}")
    print(line)
    print(f"  Total Trades    : {metrics.get('total_trades', 0):,}")
    print(f"  Avg Trades/Day  : {metrics.get('avg_per_day', 0):.1f}  "
          f"(max {metrics.get('max_per_day', 0)} on one day)")
    print(f"  Trading Days    : {metrics.get('trading_days', 0)}")
    print(f"  Win Rate        : {metrics.get('win_rate_pct', 0):.1f}%")
    print(f"  Total PnL       : ₹{metrics.get('total_pnl', 0):,.0f}")
    print(f"  Net Return      : {metrics.get('net_return_pct', 0):.2f}%")
    print(f"  CAGR            : {metrics.get('cagr_pct', 0):.2f}%")
    print(f"  Sharpe Ratio    : {metrics.get('sharpe', 0):.3f}")
    print(f"  Max Drawdown    : {metrics.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Avg Winner      : ₹{metrics.get('avg_winner', 0):,.0f}")
    print(f"  Avg Loser       : ₹{metrics.get('avg_loser', 0):,.0f}")
    print(f"  Profit Factor   : {metrics.get('profit_factor', 0):.3f}")
    print(f"  Avg Green Bars  : {metrics.get('avg_green_bars', 0):.1f}")
    print(f"  Stops Hit       : {metrics.get('stops_hit', 0):,}")
    print(f"  Time Exits      : {metrics.get('time_exits', 0):,}")
    print(f"  Final Capital   : ₹{metrics.get('final_capital', 0):,.0f}")
    print(line)

    if not sym_df.empty:
        active = sym_df[sym_df["total_trades"] > 0].copy()

        print("\n  Top 10 Symbols by PnL:")
        print(f"  {'Symbol':<28} {'Trades':>6} {'Win%':>6} {'PnL (₹)':>10}")
        print(f"  {'-'*28} {'-'*6} {'-'*6} {'-'*10}")
        for _, r in active.nlargest(10, "total_pnl").iterrows():
            print(f"  {r['symbol']:<28} {int(r['total_trades']):>6} "
                  f"{r['win_rate_pct']:>5.1f}% {r['total_pnl']:>10,.0f}")

        print("\n  Bottom 5 Symbols by PnL:")
        print(f"  {'Symbol':<28} {'Trades':>6} {'Win%':>6} {'PnL (₹)':>10}")
        print(f"  {'-'*28} {'-'*6} {'-'*6} {'-'*10}")
        for _, r in active.nsmallest(5, "total_pnl").iterrows():
            print(f"  {r['symbol']:<28} {int(r['total_trades']):>6} "
                  f"{r['win_rate_pct']:>5.1f}% {r['total_pnl']:>10,.0f}")

        print(f"\n  Symbols with trades    : {len(active)}")
        print(f"  Symbols with no trades : {len(sym_df) - len(active)}")

    print(f"\n  Trades CSV : {TRADES_CSV}")
    print(f"  Summary CSV: {SUMMARY_CSV}")
    print(f"{line}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t_start = time.perf_counter()
    print("=" * 62)
    print("  5 EMA Short — Gap-Up Reversal Backtest")
    print("=" * 62)

    loader  = DataLoader()
    symbols = loader.available_symbols()
    max_tpd = CONFIG["max_trades_per_day"]

    print(f"\n  Universe   : {len(symbols)} symbols")
    print(f"  Gap-up     : ≥ {CONFIG['gap_up_pct']*100:.1f}% open vs prev close")
    print(f"  Setup      : bar1+bar2 green+above {CONFIG['ema_period']} EMA | "
          f"bar3+ above EMA (continuous streak)")
    print(f"  Signal     : first bar3+ with range ≤ {CONFIG['max_trigger_candle_pct']*100:.1f}% "
          f"AND volume < {int(CONFIG['signal_vol_ratio']*100)}% of bar-1")
    print(f"  Entry      : short below signal candle low")
    print(f"  Cutoff     : no entries from {CONFIG['no_entry_after']} onwards")
    print(f"  Max/day    : {max_tpd} trades  (₹{CONFIG['risk_per_trade']:,} each)\n")

    all_trades: list = []
    sym_rows:   list = []
    failed:     list = []

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
                sym_rows.append({
                    "symbol":       symbol,
                    "total_trades": 0,
                    "win_rate_pct": 0.0,
                    "total_pnl":    0.0,
                })

        except Exception as exc:
            logger.warning(f"SKIP {symbol}: {exc}")
            failed.append(symbol)

        if i % 20 == 0 or i == len(symbols):
            print(f"  [{i:>3}/{len(symbols)}] {len(all_trades):>5} raw signals  "
                  f"({time.perf_counter()-t_start:.1f}s)")

    if not all_trades:
        print("\n  No trades generated.")
        return

    # ── Apply daily trade cap (keep first N entries per day by time) ──────────
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

    # ── Per-symbol summary on capped trades ───────────────────────────────────
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
    print(f"    Raw signals  : {len(raw_df):,}")
    print(f"    After cap    : {len(capped_df):,}")
    print(f"    Trading days : {len(daily_counts)}")
    print(f"    Avg/day      : {daily_counts.mean():.1f}")
    print(f"    Max/day      : {daily_counts.max()}")

    # ── Portfolio metrics ─────────────────────────────────────────────────────
    metrics = compute_metrics(capped_df, CONFIG["capital"])

    # ── Save CSVs ─────────────────────────────────────────────────────────────
    capped_df.drop(columns=["entry_date"], errors="ignore").to_csv(TRADES_CSV,  index=False)
    sym_capped.to_csv(SUMMARY_CSV, index=False)

    # ── Print results ─────────────────────────────────────────────────────────
    sym_df_full = pd.DataFrame(sym_rows)
    print_summary(metrics, sym_capped, len(symbols) - len(failed))

    if failed:
        print(f"  Skipped ({len(failed)} symbols): {failed[:5]}{'...' if len(failed) > 5 else ''}")

    print(f"  Total runtime: {time.perf_counter()-t_start:.1f}s\n")


if __name__ == "__main__":
    main()
