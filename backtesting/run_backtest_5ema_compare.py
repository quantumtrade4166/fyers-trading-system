# ============================================================
# backtesting/run_backtest_5ema_compare.py
#
# 5 EMA Short — Multi-Config Comparison
#
# Tests 5 configs side-by-side to find the best levers:
#   Baseline : 3 bars | 0.8% gap | cutoff 10:30
#   V_bars   : 5 bars | 0.8% gap | cutoff 10:30  (more streak)
#   V_gap    : 3 bars | 1.5% gap | cutoff 10:30  (bigger gap)
#   V_time   : 3 bars | 0.8% gap | cutoff 10:00  (tighter window)
#   V_combo  : 5 bars | 1.5% gap | cutoff 10:00  (all three)
#
# Strategy:
#   All symbols are loaded ONCE into memory, then each config
#   is run against the cached data — much faster than loading
#   182 symbols 5 separate times.
#
# Run: python backtesting/run_backtest_5ema_compare.py
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

from backtesting.data_loader         import DataLoader
from backtesting.strategy_5ema_short import run_symbol, DEFAULT_CONFIG

# ── Configs ───────────────────────────────────────────────────────────────────

BASE = {
    **DEFAULT_CONFIG,
    "capital":            1_000_000,
    "risk_per_trade":     2_000,
    "max_position_pct":   0.20,
    "exit_time":          "15:00",
    "max_trades_per_day": 5,
}

CONFIGS = {
    "Baseline  3b|0.8%|10:30": {**BASE, "min_green_bars": 3, "gap_up_pct": 0.008, "no_entry_after": "10:30"},
    "V_bars    5b|0.8%|10:30": {**BASE, "min_green_bars": 5, "gap_up_pct": 0.008, "no_entry_after": "10:30"},
    "V_gap     3b|1.5%|10:30": {**BASE, "min_green_bars": 3, "gap_up_pct": 0.015, "no_entry_after": "10:30"},
    "V_time    3b|0.8%|10:00": {**BASE, "min_green_bars": 3, "gap_up_pct": 0.008, "no_entry_after": "10:00"},
    "V_combo   5b|1.5%|10:00": {**BASE, "min_green_bars": 5, "gap_up_pct": 0.015, "no_entry_after": "10:00"},
}

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
RUN_TS = datetime.now().strftime("%Y%m%d_%H%M")

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s  %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("5ema_compare")


# ── Helpers ───────────────────────────────────────────────────────────────────

def apply_daily_cap(trades: list, max_per_day: int) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame([t.to_dict() for t in trades])
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["entry_date"] = df["entry_time"].dt.date
    return (
        df.sort_values("entry_time")
          .groupby("entry_date", group_keys=False)
          .head(max_per_day)
          .reset_index(drop=True)
    )


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
    }


def run_one_config(label: str, cfg: dict, symbol_data: dict) -> tuple[dict, pd.DataFrame]:
    """Run one config across all pre-loaded symbols."""
    t0         = time.perf_counter()
    all_trades = []
    failed     = []
    symbols    = list(symbol_data.keys())

    for i, (symbol, df_5min) in enumerate(symbol_data.items(), 1):
        try:
            trades = run_symbol(symbol, df_5min, cfg)
            all_trades.extend(trades)
        except Exception as exc:
            logger.warning(f"SKIP {symbol}: {exc}")
            failed.append(symbol)

        if i % 60 == 0 or i == len(symbols):
            print(f"    [{i:>3}/{len(symbols)}] {len(all_trades):>4} raw  "
                  f"({time.perf_counter()-t0:.1f}s)")

    capped  = apply_daily_cap(all_trades, cfg["max_trades_per_day"])
    metrics = compute_metrics(capped, cfg["capital"])

    # Save CSV
    tag = label.replace(" ", "_").replace("|", "-").replace(":", "")
    capped.drop(columns=["entry_date"], errors="ignore").to_csv(
        RESULTS_DIR / f"5ema_{tag}_{RUN_TS}.csv", index=False
    )

    raw_n = len(all_trades)
    cap_n = len(capped)
    print(f"    → raw {raw_n:,}  |  capped {cap_n:,}  |  "
          f"win {metrics.get('win_rate_pct',0):.1f}%  |  "
          f"PnL ₹{metrics.get('total_pnl',0):,.0f}  |  "
          f"Sharpe {metrics.get('sharpe',0):.3f}")

    return metrics, capped


def print_comparison(results: dict[str, dict]):
    labels  = list(results.keys())
    metrics = list(results.values())

    rows = [
        ("Total Trades",    "total_trades",     "{:,}"),
        ("Avg Trades/Day",  "avg_per_day",       "{:.1f}"),
        ("Win Rate",        "win_rate_pct",      "{:.1f}%"),
        ("Total PnL",       "total_pnl",         "₹{:,.0f}"),
        ("Net Return",      "net_return_pct",    "{:.2f}%"),
        ("CAGR",            "cagr_pct",          "{:.2f}%"),
        ("Sharpe Ratio",    "sharpe",            "{:.3f}"),
        ("Max Drawdown",    "max_drawdown_pct",  "{:.2f}%"),
        ("Avg Winner",      "avg_winner",        "₹{:,.0f}"),
        ("Avg Loser",       "avg_loser",         "₹{:,.0f}"),
        ("Profit Factor",   "profit_factor",     "{:.3f}"),
        ("Stops Hit",       "stops_hit",         "{:,}"),
        ("Time Exits",      "time_exits",        "{:,}"),
        ("Final Capital",   "final_capital",     "₹{:,.0f}"),
    ]

    col_w = 24
    line  = "=" * (22 + col_w * len(labels))

    print(f"\n{line}")
    print("  COMPARISON — 5 EMA Short variations")
    print(line)

    # Header (two lines — label is long)
    hdr1 = f"  {'Metric':<20}"
    hdr2 = f"  {'':<20}"
    for lbl in labels:
        parts = lbl.split()
        hdr1 += f"{parts[0]:^{col_w}}"
        hdr2 += f"{parts[1] if len(parts) > 1 else '':^{col_w}}"
    print(hdr1)
    print(hdr2)
    print(f"  {'-'*20}  " + ("  ".join(["-"*(col_w-2)] * len(labels))))

    for (name, key, fmt) in rows:
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


def print_top_symbols(label: str, capped_df: pd.DataFrame, n: int = 5):
    if capped_df.empty:
        return
    sym = (
        capped_df.groupby("symbol")
        .apply(lambda g: pd.Series({
            "trades":  len(g),
            "win_pct": round((g["pnl"] > 0).mean() * 100, 1),
            "pnl":     round(g["pnl"].sum(), 0),
        }), include_groups=False)
        .reset_index()
        .sort_values("pnl", ascending=False)
    )
    print(f"  Top {n} — {label}:")
    print(f"  {'Symbol':<26} {'Trades':>6} {'Win%':>6} {'PnL':>10}")
    print(f"  {'-'*26} {'-'*6} {'-'*6} {'-'*10}")
    for _, r in sym.head(n).iterrows():
        print(f"  {r['symbol']:<26} {int(r['trades']):>6} "
              f"{r['win_pct']:>5.1f}% {r['pnl']:>10,.0f}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t_start = time.perf_counter()

    print("=" * 62)
    print("  5 EMA Short — Multi-Config Comparison")
    print("=" * 62)

    loader  = DataLoader()
    symbols = loader.available_symbols()

    # ── Step 1: Load all symbol data ONCE into memory ─────────────────────────
    print(f"\n  Loading {len(symbols)} symbols into memory (once for all configs)...")
    t_load     = time.perf_counter()
    symbol_data: dict = {}
    failed_load: list = []

    for i, symbol in enumerate(symbols, 1):
        try:
            symbol_data[symbol] = loader.load(symbol)
        except Exception as exc:
            logger.warning(f"SKIP {symbol}: {exc}")
            failed_load.append(symbol)

        if i % 40 == 0 or i == len(symbols):
            print(f"  [{i:>3}/{len(symbols)}] loaded  ({time.perf_counter()-t_load:.1f}s)")

    n_loaded = len(symbol_data)
    print(f"\n  Loaded {n_loaded} symbols in {time.perf_counter()-t_load:.1f}s\n")
    if failed_load:
        print(f"  Skipped {len(failed_load)}: {failed_load}")

    # ── Step 2: Run each config against cached data ───────────────────────────
    all_metrics: dict[str, dict]        = {}
    all_caps:    dict[str, pd.DataFrame] = {}

    for label, cfg in CONFIGS.items():
        print(f"\n  ── {label} ──")
        metrics, capped = run_one_config(label, cfg, symbol_data)
        all_metrics[label] = metrics
        all_caps[label]    = capped

    # ── Step 3: Comparison table ──────────────────────────────────────────────
    print_comparison(all_metrics)

    # ── Step 4: Top symbols for best-performing config ────────────────────────
    best_label = max(all_metrics, key=lambda l: all_metrics[l].get("sharpe", -999))
    print(f"\n  Best config by Sharpe: {best_label}")
    print_top_symbols(best_label, all_caps[best_label], n=5)

    print(f"  Total runtime: {time.perf_counter()-t_start:.1f}s\n")


if __name__ == "__main__":
    main()
