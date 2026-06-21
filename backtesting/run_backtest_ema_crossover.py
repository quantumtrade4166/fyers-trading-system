# ============================================================
# backtesting/run_backtest_ema_crossover.py
#
# 5/9 EMA Crossover — Intraday Backtest Runner
#
# Strategy recap:
#   Signal : 5 EMA crosses above/below 9 EMA on 5-min bar
#   Entry  : next bar breaks signal candle HIGH (long) / LOW (short)
#   SL     : signal candle LOW (long) / HIGH (short)
#   Exit   : SL hit  OR  close of 15:10 bar
#   Filter : no new signals at/after 14:45
#            one trade per symbol per day
#   Cap    : 3 long + 3 short per day (first by entry time, portfolio-level)
#   Risk   : ₹10,000 / 6 slots = ₹1,667 per trade
#
# Run: python backtesting/run_backtest_ema_crossover.py
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

from backtesting.data_loader          import DataLoader
from backtesting.strategy_ema_crossover import run_symbol, DEFAULT_CONFIG

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG = {
    **DEFAULT_CONFIG,
    "fast_ema":           5,
    "slow_ema":           9,
    "capital":            1_000_000,
    "risk_per_trade":     1_667,      # ₹10,000 / 6 daily slots
    "max_position_pct":   0.20,
    "exit_time":          "15:10",
    "no_entry_after":     "14:45",
    "max_long_per_day":   3,
    "max_short_per_day":  3,
}

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
RUN_TS      = datetime.now().strftime("%Y%m%d_%H%M")
TRADES_CSV  = RESULTS_DIR / f"ema_cross_trades_{RUN_TS}.csv"
SUMMARY_CSV = RESULTS_DIR / f"ema_cross_summary_{RUN_TS}.csv"

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s  %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("run_ema_cross")


# ── Daily cap ─────────────────────────────────────────────────────────────────

def apply_daily_cap(df: pd.DataFrame,
                    max_long: int, max_short: int) -> pd.DataFrame:
    """
    Keep the first `max_long` long trades and first `max_short` short trades
    per calendar day, ordered by entry time.
    """
    if df.empty:
        return df

    df = df.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["entry_date"] = df["entry_time"].dt.date
    df = df.sort_values("entry_time")

    longs  = (df[df["direction"] == "long"]
              .groupby("entry_date", group_keys=False)
              .head(max_long))
    shorts = (df[df["direction"] == "short"]
              .groupby("entry_date", group_keys=False)
              .head(max_short))

    return (pd.concat([longs, shorts])
              .sort_values("entry_time")
              .reset_index(drop=True))


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(df: pd.DataFrame, capital: float) -> dict:
    if df.empty:
        return {}

    df = df.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"]  = pd.to_datetime(df["exit_time"])

    total  = len(df)
    longs  = df[df["direction"] == "long"]
    shorts = df[df["direction"] == "short"]
    wins   = df[df["pnl"] > 0]
    losses = df[df["pnl"] < 0]

    total_pnl    = df["pnl"].sum()
    win_rate     = len(wins) / total * 100
    avg_win      = wins["pnl"].mean()   if len(wins)   else 0
    avg_loss     = losses["pnl"].mean() if len(losses)  else 0
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

    reason       = df["exit_reason"].value_counts().to_dict()
    daily_counts = df.groupby("exit_date").size()

    return {
        "total_trades":     total,
        "long_trades":      len(longs),
        "short_trades":     len(shorts),
        "avg_per_day":      round(daily_counts.mean(), 1),
        "win_rate_pct":     round(win_rate, 1),
        "long_win_pct":     round((longs["pnl"] > 0).mean() * 100, 1) if len(longs) else 0,
        "short_win_pct":    round((shorts["pnl"] > 0).mean() * 100, 1) if len(shorts) else 0,
        "total_pnl":        round(total_pnl, 0),
        "net_return_pct":   round(total_pnl / capital * 100, 2),
        "cagr_pct":         round(cagr, 2),
        "sharpe":           round(sharpe, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "avg_winner":       round(avg_win, 0),
        "avg_loser":        round(avg_loss, 0),
        "profit_factor":    round(pf, 3),
        "stops_hit":        reason.get("stop", 0) + reason.get("stop_same_bar", 0),
        "time_exits":       reason.get("time_exit", 0),
        "trading_days":     len(daily_counts),
        "date_start":       str(date_start),
        "date_end":         str(date_end),
        "final_capital":    round(final_cap, 0),
    }


# ── Print summary ──────────────────────────────────────────────────────────────

def print_summary(metrics: dict, sym_df: pd.DataFrame, n_symbols: int):
    line = "=" * 62
    print(f"\n{line}")
    print("  5/9 EMA CROSSOVER — INTRADAY STRATEGY")
    print(line)
    print(f"  Signal    : 5 EMA crosses above/below 9 EMA (5-min)")
    print(f"  Entry     : next bar breaks signal candle high (L) / low (S)")
    print(f"  SL        : signal candle low (L) / high (S)")
    print(f"  Exit      : SL hit  OR  {CONFIG['exit_time']} bar close")
    print(f"  Cutoff    : no new signals at/after {CONFIG['no_entry_after']}")
    print(f"  Universe  : {n_symbols} symbols (5-min bars)")
    print(f"  Period    : {metrics.get('date_start')} → {metrics.get('date_end')}")
    print(f"  Capital   : ₹{CONFIG['capital']:,.0f}  |  "
          f"Risk/trade: ₹{CONFIG['risk_per_trade']:,}  |  "
          f"Max: {CONFIG['max_long_per_day']}L + {CONFIG['max_short_per_day']}S /day")
    print(line)
    print(f"  Total Trades    : {metrics.get('total_trades', 0):,}  "
          f"({metrics.get('long_trades', 0)} long / {metrics.get('short_trades', 0)} short)")
    print(f"  Avg Trades/Day  : {metrics.get('avg_per_day', 0):.1f}")
    print(f"  Trading Days    : {metrics.get('trading_days', 0)}")
    print(f"  Win Rate        : {metrics.get('win_rate_pct', 0):.1f}%  "
          f"(L: {metrics.get('long_win_pct', 0):.1f}%  "
          f"S: {metrics.get('short_win_pct', 0):.1f}%)")
    print(f"  Total PnL       : ₹{metrics.get('total_pnl', 0):,.0f}")
    print(f"  Net Return      : {metrics.get('net_return_pct', 0):.2f}%")
    print(f"  CAGR            : {metrics.get('cagr_pct', 0):.2f}%")
    print(f"  Sharpe Ratio    : {metrics.get('sharpe', 0):.3f}")
    print(f"  Max Drawdown    : {metrics.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Avg Winner      : ₹{metrics.get('avg_winner', 0):,.0f}")
    print(f"  Avg Loser       : ₹{metrics.get('avg_loser', 0):,.0f}")
    print(f"  Profit Factor   : {metrics.get('profit_factor', 0):.3f}")
    print(f"  Stops Hit       : {metrics.get('stops_hit', 0):,}")
    print(f"  Time Exits      : {metrics.get('time_exits', 0):,}")
    print(f"  Final Capital   : ₹{metrics.get('final_capital', 0):,.0f}")
    print(line)

    if not sym_df.empty:
        active = sym_df[sym_df["total_trades"] > 0].copy()
        print("\n  Top 10 Symbols by PnL:")
        print(f"  {'Symbol':<28} {'Trades':>6} {'L/S':>5} {'Win%':>6} {'PnL (₹)':>10}")
        print(f"  {'-'*28} {'-'*6} {'-'*5} {'-'*6} {'-'*10}")
        for _, r in active.nlargest(10, "total_pnl").iterrows():
            print(f"  {r['symbol']:<28} {int(r['total_trades']):>6} "
                  f"{int(r['long_trades']):>2}L/{int(r['short_trades'])}S "
                  f"{r['win_rate_pct']:>5.1f}% {r['total_pnl']:>10,.0f}")

        print("\n  Bottom 5 Symbols by PnL:")
        print(f"  {'Symbol':<28} {'Trades':>6} {'L/S':>5} {'Win%':>6} {'PnL (₹)':>10}")
        print(f"  {'-'*28} {'-'*6} {'-'*5} {'-'*6} {'-'*10}")
        for _, r in active.nsmallest(5, "total_pnl").iterrows():
            print(f"  {r['symbol']:<28} {int(r['total_trades']):>6} "
                  f"{int(r['long_trades']):>2}L/{int(r['short_trades'])}S "
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
    print("  5/9 EMA Crossover — Intraday Backtest")
    print("=" * 62)

    loader  = DataLoader()
    symbols = loader.available_symbols()
    max_L   = CONFIG["max_long_per_day"]
    max_S   = CONFIG["max_short_per_day"]

    print(f"\n  Universe   : {len(symbols)} symbols")
    print(f"  Fast EMA   : {CONFIG['fast_ema']} | Slow EMA: {CONFIG['slow_ema']}")
    print(f"  Entry      : next bar breaks signal candle H (long) / L (short)")
    print(f"  Cutoff     : no new signals from {CONFIG['no_entry_after']} onwards")
    print(f"  Exit       : SL or {CONFIG['exit_time']} bar close")
    print(f"  Max/day    : {max_L}L + {max_S}S  (₹{CONFIG['risk_per_trade']:,} each)\n")

    all_trades: list = []
    sym_rows:   list = []
    failed:     list = []

    for i, symbol in enumerate(symbols, 1):
        try:
            df_5min = loader.load(symbol)
            trades  = run_symbol(symbol, df_5min, CONFIG)

            n = len(trades)
            if n > 0:
                tdf = pd.DataFrame([t.to_dict() for t in trades])
                sym_rows.append({
                    "symbol":       symbol,
                    "total_trades": n,
                    "long_trades":  (tdf["direction"] == "long").sum(),
                    "short_trades": (tdf["direction"] == "short").sum(),
                    "win_rate_pct": round((tdf["pnl"] > 0).mean() * 100, 1),
                    "total_pnl":    round(tdf["pnl"].sum(), 0),
                })
                all_trades.extend(trades)
            else:
                sym_rows.append({
                    "symbol": symbol, "total_trades": 0,
                    "long_trades": 0, "short_trades": 0,
                    "win_rate_pct": 0.0, "total_pnl": 0.0,
                })

        except Exception as exc:
            logger.warning(f"SKIP {symbol}: {exc}")
            failed.append(symbol)

        if i % 20 == 0 or i == len(symbols):
            print(f"  [{i:>3}/{len(symbols)}] {len(all_trades):>6} raw signals  "
                  f"({time.perf_counter()-t_start:.1f}s)")

    if not all_trades:
        print("\n  No trades generated.")
        return

    # ── Apply 3L + 3S daily cap ───────────────────────────────────────────────
    print(f"\n  Applying {max_L}L + {max_S}S per day cap...")
    raw_df = pd.DataFrame([t.to_dict() for t in all_trades])
    raw_df["entry_time"] = pd.to_datetime(raw_df["entry_time"])

    capped_df = apply_daily_cap(raw_df, max_L, max_S)

    daily_counts = capped_df.groupby(capped_df["entry_time"].dt.date).size()
    print(f"\n  After cap:")
    print(f"    Raw signals  : {len(raw_df):,}")
    print(f"    After cap    : {len(capped_df):,}")
    print(f"    Long trades  : {(capped_df['direction']=='long').sum():,}")
    print(f"    Short trades : {(capped_df['direction']=='short').sum():,}")
    print(f"    Trading days : {len(daily_counts)}")
    print(f"    Avg/day      : {daily_counts.mean():.1f}")
    print(f"    Max/day      : {daily_counts.max()}")

    # ── Per-symbol summary on capped trades ───────────────────────────────────
    sym_capped = (
        capped_df.groupby("symbol")
        .apply(lambda g: pd.Series({
            "total_trades": len(g),
            "long_trades":  (g["direction"] == "long").sum(),
            "short_trades": (g["direction"] == "short").sum(),
            "win_rate_pct": round((g["pnl"] > 0).mean() * 100, 1),
            "total_pnl":    round(g["pnl"].sum(), 0),
        }), include_groups=False)
        .reset_index()
    )

    # ── Portfolio metrics ─────────────────────────────────────────────────────
    metrics = compute_metrics(capped_df, CONFIG["capital"])

    # ── Save CSVs ─────────────────────────────────────────────────────────────
    capped_df.drop(columns=["entry_date"], errors="ignore").to_csv(TRADES_CSV,  index=False)
    sym_capped.to_csv(SUMMARY_CSV, index=False)

    # ── Print results ─────────────────────────────────────────────────────────
    print_summary(metrics, sym_capped, len(symbols) - len(failed))

    if failed:
        print(f"  Skipped ({len(failed)} symbols): {failed[:5]}{'...' if len(failed) > 5 else ''}")

    print(f"  Total runtime: {time.perf_counter()-t_start:.1f}s\n")


if __name__ == "__main__":
    main()
