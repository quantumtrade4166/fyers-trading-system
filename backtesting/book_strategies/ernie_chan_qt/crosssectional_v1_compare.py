# ============================================================
# backtesting/book_strategies/ernie_chan_qt/crosssectional_v1_compare.py
#
# Compares 4 variants of Cross-Sectional Mean Reversion V1:
#   A) Baseline   : divisor=n_eligible, min_dev=0.5%  ← already known
#   B) Div by 20  : divisor=20,         min_dev=0.5%
#   C) High dev   : divisor=n_eligible, min_dev=1.5%
#   D) Both       : divisor=20,         min_dev=1.5%
#
# Data loads once; all 4 simulations share the same daily panels.
#
# Usage:
#   G:\fyers_data_pipeline\.venv\Scripts\python.exe
#       backtesting\book_strategies\ernie_chan_qt\crosssectional_v1_compare.py
# ============================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv

# ─────────────────────────────────────────────────────────────────────────────
# FIXED PARAMETERS (same across all variants)
# ─────────────────────────────────────────────────────────────────────────────
TOTAL_CAPITAL     = 10_000_000     # ₹1 crore
N                 = 10             # top-N longs + top-N shorts
MAX_PER_STOCK_PCT = 0.10
LEVERAGE          = 1
BROKERAGE_ONEWAY  = 0.0003
STT_SELL_SIDE     = 0.00025

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGS  (what we're varying)
# ─────────────────────────────────────────────────────────────────────────────
CONFIGS = [
    {"label": "A — Baseline",      "min_dev": 0.005, "fixed_divisor": None},
    {"label": "B — Div by 20",     "min_dev": 0.005, "fixed_divisor": 20},
    {"label": "C — High dev 1.5%", "min_dev": 0.015, "fixed_divisor": None},
    {"label": "D — Both",          "min_dev": 0.015, "fixed_divisor": 20},
]


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOAD (once)
# ─────────────────────────────────────────────────────────────────────────────

def load_daily_panels(loader: DataLoader):
    symbols  = loader.available_symbols()
    print(f"Loading {len(symbols)} symbols...")
    raw_data = loader.load_many(symbols)
    print(f"Loaded: {len(raw_data)} / {len(symbols)} symbols\n")

    print("Resampling to daily...")
    closes_dict, opens_dict = {}, {}
    for sym, df in raw_data.items():
        try:
            daily = resample_ohlcv(df, "1D")
            if len(daily) < 5:
                continue
            daily.index = daily.index.normalize()
            closes_dict[sym] = daily["close"]
            opens_dict[sym]  = daily["open"]
        except Exception as exc:
            print(f"  Skipped {sym}: {exc}")

    daily_closes = pd.DataFrame(closes_dict).sort_index()
    daily_opens  = pd.DataFrame(opens_dict).sort_index()
    all_dates    = daily_closes.index.union(daily_opens.index)
    daily_closes = daily_closes.reindex(all_dates)
    daily_opens  = daily_opens.reindex(all_dates)

    print(f"Panels ready: {daily_closes.shape[1]} symbols, "
          f"{daily_closes.index[0].date()} → {daily_closes.index[-1].date()}\n")
    return daily_closes, daily_opens


# ─────────────────────────────────────────────────────────────────────────────
# SIMULATION  (parameterised)
# ─────────────────────────────────────────────────────────────────────────────

def simulate(daily_closes, daily_opens, min_dev: float, fixed_divisor):
    """
    fixed_divisor=None  → divide by n_eligible (book default)
    fixed_divisor=20    → always divide by 2*N (forces full deployment)
    """
    trading_dates = sorted(daily_closes.index)
    max_per_stock = TOTAL_CAPITAL * MAX_PER_STOCK_PCT * LEVERAGE

    daily_records = []
    trade_records = []

    for i, trade_date in enumerate(trading_dates):
        if i < 2:
            continue

        signal_date = trading_dates[i - 1]
        prev_date   = trading_dates[i - 2]

        close_yesterday  = daily_closes.loc[signal_date]
        close_day_before = daily_closes.loc[prev_date]
        open_today       = daily_opens.loc[trade_date]
        close_today      = daily_closes.loc[trade_date]

        valid_mask = (
            close_yesterday.notna()  &
            close_day_before.notna() &
            open_today.notna()       &
            close_today.notna()      &
            (close_day_before > 0)   &
            (open_today > 0)         &
            (close_today > 0)
        )
        valid_syms = close_yesterday[valid_mask].index
        if len(valid_syms) < 2 * N:
            continue

        ret        = ((close_yesterday[valid_syms] - close_day_before[valid_syms])
                      / close_day_before[valid_syms])
        market_avg = ret.mean()
        deviation  = ret - market_avg

        filtered   = deviation[deviation.abs() >= min_dev]
        n_eligible = len(filtered)
        if n_eligible < 2 * N:
            continue

        divisor    = fixed_divisor if fixed_divisor is not None else n_eligible

        sorted_dev = filtered.sort_values()
        longs      = sorted_dev.iloc[:N]
        shorts     = sorted_dev.iloc[-N:]
        positions  = pd.concat([longs, shorts])

        day_net_pnl = 0.0
        day_trades  = 0

        for sym, dev in positions.items():
            weight       = -dev / divisor
            raw_position = weight * TOTAL_CAPITAL * LEVERAGE
            capped       = float(np.clip(raw_position, -max_per_stock, max_per_stock))

            entry_price = float(open_today[sym])
            exit_price  = float(close_today[sym])

            quantity = int(abs(capped) / entry_price)
            if quantity == 0:
                continue
            if quantity * entry_price > TOTAL_CAPITAL * LEVERAGE:
                continue

            is_long         = dev < 0
            tv_entry        = quantity * entry_price
            tv_exit         = quantity * exit_price

            if is_long:
                gross_pnl = (exit_price - entry_price) * quantity
                stt       = tv_exit  * STT_SELL_SIDE
            else:
                gross_pnl = (entry_price - exit_price) * quantity
                stt       = tv_entry * STT_SELL_SIDE

            cost    = (tv_entry + tv_exit) * BROKERAGE_ONEWAY + stt
            net_pnl = gross_pnl - cost

            day_net_pnl += net_pnl
            day_trades  += 1

            trade_records.append({
                "date": trade_date.date(), "symbol": sym,
                "direction": "LONG" if is_long else "SHORT",
                "net_pnl": round(net_pnl, 2),
            })

        daily_records.append({
            "date": trade_date.date(),
            "trades": day_trades,
            "pnl_net": round(day_net_pnl, 2),
        })

    return pd.DataFrame(daily_records), pd.DataFrame(trade_records)


# ─────────────────────────────────────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(daily_df, trade_df):
    daily_df         = daily_df.copy()
    daily_df["equity"]    = TOTAL_CAPITAL + daily_df["pnl_net"].cumsum()
    daily_df["daily_ret"] = daily_df["pnl_net"] / TOTAL_CAPITAL

    mean_ret = daily_df["daily_ret"].mean()
    std_ret  = daily_df["daily_ret"].std()
    sharpe   = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0.0

    equity      = daily_df["equity"]
    rolling_max = equity.cummax()
    drawdown    = (equity - rolling_max) / rolling_max * 100
    max_dd_pct  = drawdown.min()
    daily_df["drawdown_pct"] = drawdown

    max_dd_days = cur = 0
    for in_dd in (drawdown < 0):
        cur = cur + 1 if in_dd else 0
        max_dd_days = max(max_dd_days, cur)

    total_trades = len(trade_df)
    win_rate     = (trade_df["net_pnl"] > 0).mean() * 100 if total_trades else 0.0

    return daily_df, dict(
        sharpe=sharpe, max_dd_pct=max_dd_pct, max_dd_days=max_dd_days,
        total_trades=total_trades, win_rate=win_rate,
        net_pnl=daily_df["pnl_net"].sum(),
        avg_daily=daily_df["pnl_net"].mean(),
        total_days=len(daily_df),
    )


# ─────────────────────────────────────────────────────────────────────────────
# COMPARISON PLOT
# ─────────────────────────────────────────────────────────────────────────────

def save_comparison_plot(results: list):
    """
    results: list of (config_label, daily_df_with_equity, metrics)
    """
    colors = ["#1565C0", "#2E7D32", "#E65100", "#6A1B9A"]
    fig, axes = plt.subplots(2, 1, figsize=(15, 9),
                             gridspec_kw={"height_ratios": [3, 1]}, sharex=True)

    ax1, ax2 = axes

    for (label, daily_df, m), color in zip(results, colors):
        dates = pd.to_datetime(daily_df["date"])
        ax1.plot(dates, daily_df["equity"], label=label, color=color, linewidth=1.5)
        ax2.plot(dates, daily_df["drawdown_pct"], color=color, linewidth=1.0, alpha=0.8)

    ax1.axhline(TOTAL_CAPITAL, color="#9E9E9E", linestyle="--",
                linewidth=0.8, alpha=0.6, label="Starting Capital")
    ax1.set_title(
        "Cross-Sectional Mean Reversion — Variant Comparison\n"
        f"Capital ₹{TOTAL_CAPITAL/1e7:.0f}Cr | N={N} | Open-to-Close",
        fontsize=12, fontweight="bold",
    )
    ax1.set_ylabel("Portfolio Value (₹)", fontsize=10)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x/1e6:.1f}M"))
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(True, alpha=0.25)

    ax2.axhline(0, color="#9E9E9E", linestyle="--", linewidth=0.7)
    ax2.set_ylabel("Drawdown %", fontsize=9)
    ax2.set_xlabel("Date", fontsize=9)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax2.grid(True, alpha=0.25)

    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    out = RESULTS_DIR / "comparison_v1.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\nComparison plot → {out}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("Cross-Sectional Mean Reversion — Variant Comparison")
    print(f"Capital: ₹{TOTAL_CAPITAL:,}  |  N={N}  |  Leverage={LEVERAGE}")
    print("=" * 65)

    loader = DataLoader()
    daily_closes, daily_opens = load_daily_panels(loader)

    results = []
    summary_rows = []

    for cfg in CONFIGS:
        label        = cfg["label"]
        min_dev      = cfg["min_dev"]
        fixed_div    = cfg["fixed_divisor"]
        div_label    = str(fixed_div) if fixed_div else "n_elig"

        print(f"Running {label}  (min_dev={min_dev*100:.1f}%, divisor={div_label})...")
        daily_df, trade_df = simulate(daily_closes, daily_opens, min_dev, fixed_div)

        if daily_df.empty:
            print(f"  → No trades generated, skipping.\n")
            continue

        daily_df, m = compute_metrics(daily_df, trade_df)
        results.append((label, daily_df, m))

        summary_rows.append({
            "Config"      : label,
            "min_dev"     : f"{min_dev*100:.1f}%",
            "divisor"     : div_label,
            "Sharpe"      : round(m["sharpe"], 3),
            "Max DD %"    : round(m["max_dd_pct"], 2),
            "Max DD Days" : m["max_dd_days"],
            "Trades"      : m["total_trades"],
            "Win %"       : round(m["win_rate"], 1),
            "Net P&L ₹"   : round(m["net_pnl"], 0),
            "Avg Daily ₹" : round(m["avg_daily"], 0),
        })

    # ── Print comparison table ────────────────────────────────────────────────
    print(f"\n{'=' * 65}")
    print("=== COMPARISON RESULTS ===")
    print(f"{'=' * 65}")

    df_summary = pd.DataFrame(summary_rows)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", "{:.3f}".format)
    print(df_summary.to_string(index=False))
    print(f"{'=' * 65}")

    # ── Individual result blocks ──────────────────────────────────────────────
    for label, _, m in results:
        print(f"\n--- {label} ---")
        print(f"  Sharpe Ratio      : {m['sharpe']:.3f}")
        print(f"  Max Drawdown %    : {m['max_dd_pct']:.2f}%")
        print(f"  Max DD Duration   : {m['max_dd_days']} days")
        print(f"  Total Trades      : {m['total_trades']:,}")
        print(f"  Win Rate %        : {m['win_rate']:.1f}%")
        print(f"  Net P&L           : ₹{m['net_pnl']:,.0f}")
        print(f"  Avg Daily P&L     : ₹{m['avg_daily']:,.0f}")
        print(f"  Total Days Tested : {m['total_days']}")

    # ── Save comparison plot ──────────────────────────────────────────────────
    if results:
        save_comparison_plot(results)

    print("\nDone.")


if __name__ == "__main__":
    main()
