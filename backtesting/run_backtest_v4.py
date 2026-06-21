# ============================================================
# backtesting/run_backtest_v4.py
#
# BB Reversion — min_outside_bars=3, two variants:
#   V4a: 3 outside bars, NO EMA filter
#   V4b: 3 outside bars, WITH 1h 50 EMA filter
#
# Runs both and prints side-by-side comparison.
# Run: python backtesting/run_backtest_v4.py
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

# ── Shared base config ────────────────────────────────────────────────────────

BASE = {
    **DEFAULT_CONFIG,
    "timeframe":             "15min",
    "bb_period":             20,
    "bb_std":                2.0,
    "min_outside_bars":      3,          # ← changed from 2 to 3
    "capital":               1_000_000,
    "risk_per_trade":        2_000,
    "max_position_pct":      0.20,
    "exit_time":             "15:00",
    "direction":             "short",
    "max_signal_candle_pct": 0.008,
    "max_trades_per_day":    5,
}

CONFIGS = {
    "V4a — 3 bars, no EMA": {
        **BASE,
        "ema_trend_filter": False,
    },
    "V4b — 3 bars + 1h EMA": {
        **BASE,
        "ema_trend_filter":    True,
        "ema_trend_period":    50,
        "ema_trend_timeframe": "1h",
    },
}

RESULTS_DIR = Path(r"G:\Trading Backtesting\results")
RESULTS_DIR.mkdir(exist_ok=True)
RUN_TS = datetime.now().strftime("%Y%m%d_%H%M")

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s  %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("run_v4")


# ── Helpers ───────────────────────────────────────────────────────────────────

def apply_daily_cap(trades: list, max_per_day: int) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame([t.to_dict() for t in trades])
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["entry_date"] = df["entry_time"].dt.date
    capped = (
        df.sort_values("entry_time")
        .groupby("entry_date", group_keys=False)
        .head(max_per_day)
        .reset_index(drop=True)
    )
    return capped


def compute_metrics(df: pd.DataFrame, capital: float) -> dict:
    if df.empty:
        return {}
    df = df.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"]  = pd.to_datetime(df["exit_time"])

    total  = len(df)
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

    reason = df["exit_reason"].value_counts().to_dict()
    daily_counts = df.groupby("exit_date").size()

    return {
        "total_trades":     total,
        "win_rate_pct":     round(win_rate, 1),
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
        "avg_per_day":      round(daily_counts.mean(), 1),
        "final_capital":    round(final_cap, 0),
        "date_start":       str(date_start),
        "date_end":         str(date_end),
    }


def run_config(label: str, cfg: dict, loader: DataLoader, symbols: list) -> tuple[dict, pd.DataFrame]:
    """Run one config across all symbols, return (metrics, capped_trades_df)."""
    print(f"\n  Running {label}...")
    t0 = time.perf_counter()

    all_trades = []
    failed     = []

    for i, symbol in enumerate(symbols, 1):
        try:
            df_5min = loader.load(symbol)
            trades  = run_symbol(symbol, df_5min, cfg)
            all_trades.extend(trades)
        except Exception as exc:
            logger.warning(f"SKIP {symbol}: {exc}")
            failed.append(symbol)

        if i % 40 == 0 or i == len(symbols):
            print(f"    [{i:>3}/{len(symbols)}] {len(all_trades):>4} raw signals  "
                  f"({time.perf_counter()-t0:.1f}s)")

    capped = apply_daily_cap(all_trades, cfg["max_trades_per_day"])
    metrics = compute_metrics(capped, cfg["capital"])

    # Save CSV
    tag = label.replace(" ", "_").replace(",", "").replace("+", "plus")
    capped.drop(columns=["entry_date"], errors="ignore").to_csv(
        RESULTS_DIR / f"bb_reversion_{tag}_{RUN_TS}.csv", index=False
    )

    print(f"    → {len(all_trades)} raw  |  {len(capped)} after cap  |  "
          f"win {metrics.get('win_rate_pct',0):.1f}%  |  "
          f"PnL ₹{metrics.get('total_pnl',0):,.0f}  |  "
          f"Sharpe {metrics.get('sharpe',0):.3f}")

    return metrics, capped


def print_comparison(results: dict[str, dict]):
    """Print a side-by-side comparison table of all configs."""
    labels  = list(results.keys())
    metrics = list(results.values())

    rows = [
        ("Total Trades",     "total_trades",     "{:,}",    ""),
        ("Avg Trades/Day",   "avg_per_day",       "{:.1f}",  ""),
        ("Win Rate",         "win_rate_pct",      "{:.1f}%", ""),
        ("Total PnL",        "total_pnl",         "₹{:,.0f}",""),
        ("Net Return",       "net_return_pct",    "{:.2f}%", ""),
        ("CAGR",             "cagr_pct",          "{:.2f}%", ""),
        ("Sharpe Ratio",     "sharpe",            "{:.3f}",  ""),
        ("Max Drawdown",     "max_drawdown_pct",  "{:.2f}%", ""),
        ("Avg Winner",       "avg_winner",        "₹{:,.0f}",""),
        ("Avg Loser",        "avg_loser",         "₹{:,.0f}",""),
        ("Profit Factor",    "profit_factor",     "{:.3f}",  ""),
        ("Stops Hit",        "stops_hit",         "{:,}",    ""),
        ("Time Exits",       "time_exits",        "{:,}",    ""),
        ("Final Capital",    "final_capital",     "₹{:,.0f}",""),
    ]

    col_w = 26
    line  = "=" * (20 + col_w * len(labels))

    print(f"\n{line}")
    print("  COMPARISON — min_outside_bars = 3")
    print(line)

    # Header
    header = f"  {'Metric':<20}"
    for lbl in labels:
        header += f"{lbl:^{col_w}}"
    print(header)
    print(f"  {'-'*18}  " + ("  ".join(["-"*(col_w-2)] * len(labels))))

    # Rows
    for (name, key, fmt, _) in rows:
        row = f"  {name:<20}"
        for m in metrics:
            val = m.get(key, 0)
            try:
                formatted = fmt.format(val)
            except Exception:
                formatted = str(val)
            row += f"{formatted:^{col_w}}"
        print(row)

    print(line)

    # Per-version top-5 symbols
    print()
    for label, (cfg_label, trades_df) in zip(labels, [(l, d) for l, d in zip(labels, [None]*len(labels))]):
        pass  # handled below


def print_top_symbols(label: str, capped_df: pd.DataFrame, n: int = 5):
    if capped_df.empty:
        return
    sym = (
        capped_df.groupby("symbol")
        .apply(lambda g: pd.Series({
            "trades":   len(g),
            "win_pct":  round((g["pnl"] > 0).mean() * 100, 1),
            "pnl":      round(g["pnl"].sum(), 0),
        }), include_groups=False)
        .reset_index()
        .sort_values("pnl", ascending=False)
    )
    print(f"  Top {n} — {label}:")
    print(f"  {'Symbol':<26} {'Trades':>6} {'Win%':>6} {'PnL':>10}")
    print(f"  {'-'*26} {'-'*6} {'-'*6} {'-'*10}")
    for _, r in sym.head(n).iterrows():
        print(f"  {r['symbol']:<26} {int(r['trades']):>6} {r['win_pct']:>5.1f}% {r['pnl']:>10,.0f}")
    print(f"  Bottom {n}:")
    for _, r in sym.tail(n).iterrows():
        print(f"  {r['symbol']:<26} {int(r['trades']):>6} {r['win_pct']:>5.1f}% {r['pnl']:>10,.0f}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t_start = time.perf_counter()
    print("=" * 62)
    print("  BB Reversion V4 — min_outside_bars=3 (two variants)")
    print("=" * 62)

    loader  = DataLoader()
    symbols = loader.available_symbols()

    all_metrics = {}
    all_caps    = {}

    for label, cfg in CONFIGS.items():
        metrics, capped = run_config(label, cfg, loader, symbols)
        all_metrics[label] = metrics
        all_caps[label]    = capped

    # ── Comparison table ──────────────────────────────────────────────────────
    print_comparison(all_metrics)

    # ── Top symbols per variant ───────────────────────────────────────────────
    for label, capped in all_caps.items():
        print_top_symbols(label, capped, n=5)

    print(f"  Total runtime: {time.perf_counter()-t_start:.1f}s\n")


if __name__ == "__main__":
    main()
