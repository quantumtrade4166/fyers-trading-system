# ============================================================
# backtesting/book_strategies/ernie_chan_qt/crosssectional_v2_overnight.py
#
# Cross-Sectional Mean Reversion — Overnight (V2)
# Source: Ernie Chan, "Quantitative Trading" (2008)
#
# This is the TRUE implementation from the book:
#   Signal  : prior-day return deviation from market avg
#   Entry   : TODAY's CLOSE  (end of signal day)
#   Exit    : TOMORROW's CLOSE  (hold overnight)
#
# V1 used open→close (intraday).  The reversion edge in the
# original paper lives in the overnight move, not intraday.
#
# ⚠️  INDIA MARKET NOTE (printed at runtime):
#   - LONG legs: equity cash delivery (CNC) — STT 0.1% each side
#   - SHORT legs: cash market shorts MUST be squared intraday
#     → overnight shorts require equity FUTURES (different margin/costs)
#   This backtest runs both legs at the user-supplied cost rate as a
#   signal-quality check.  Realistic cost comparison is printed below.
#
# Usage:
#   G:\fyers_data_pipeline\.venv\Scripts\python.exe
#       backtesting\book_strategies\ernie_chan_qt\crosssectional_v2_overnight.py
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
# PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────
TOTAL_CAPITAL     = 1_000_000      # ₹10 lakh
N                 = 10             # top-N longs + top-N shorts
MIN_DEVIATION     = 0.005          # 0.5%
MAX_PER_STOCK_PCT = 0.10
LEVERAGE          = 1

# ── Two cost scenarios run side-by-side ──────────────────────────────────────
# Scenario 1: intraday-equivalent (same as V1, signal-quality baseline)
#   Brokerage 0.03%/leg, STT 0.025% sell-only → 0.085% round trip
# Scenario 2: realistic delivery/futures overnight costs
#   Long CNC: STT 0.1% buy + 0.1% sell → 0.26% round trip
#   Short futures: no STT on buy, ~0.01% on sell → ~0.07% round trip
#   Blended estimate for scenario 2: 0.165% round trip (avg of long+short)
COST_SCENARIOS = {
    "Intraday costs (0.085% RT)": {
        "brokerage": 0.0003,        # per leg, both sides
        "stt_buy":   0.0000,        # no STT on buy for intraday
        "stt_sell":  0.00025,       # STT on sell side only
    },
    "Delivery costs (0.26% RT)":  {
        "brokerage": 0.0003,        # per leg
        "stt_buy":   0.001,         # 0.1% STT on buy (CNC delivery)
        "stt_sell":  0.001,         # 0.1% STT on sell (CNC delivery)
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT PATHS
# ─────────────────────────────────────────────────────────────────────────────
RESULTS_DIR       = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
EQUITY_CURVE_PATH = RESULTS_DIR / "equity_curve_v2_overnight.png"
DAILY_PNL_PATH    = RESULTS_DIR / "daily_pnl_v2_overnight.csv"


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOAD
# ─────────────────────────────────────────────────────────────────────────────

def load_daily_closes(loader: DataLoader) -> pd.DataFrame:
    """Load all symbols, resample to daily, return aligned close panel."""
    symbols  = loader.available_symbols()
    print(f"Loading {len(symbols)} symbols...")
    raw_data = loader.load_many(symbols)
    print(f"Loaded: {len(raw_data)} / {len(symbols)} symbols\n")

    print("Resampling to daily...")
    closes_dict = {}
    for sym, df in raw_data.items():
        try:
            daily = resample_ohlcv(df, "1D")
            if len(daily) < 5:
                continue
            daily.index = daily.index.normalize()
            closes_dict[sym] = daily["close"]
        except Exception as exc:
            print(f"  Skipped {sym}: {exc}")

    panel = pd.DataFrame(closes_dict).sort_index()
    print(f"Panel ready: {panel.shape[1]} symbols, "
          f"{panel.index[0].date()} → {panel.index[-1].date()}\n")
    return panel


# ─────────────────────────────────────────────────────────────────────────────
# SIMULATION
# ─────────────────────────────────────────────────────────────────────────────

def simulate(daily_closes: pd.DataFrame, costs: dict) -> tuple:
    """
    Overnight holding:
      Signal  → day[i-1] vs day[i-2] return
      Entry   → close of day[i]
      Exit    → close of day[i+1]
    """
    trading_dates = sorted(daily_closes.index)
    max_per_stock = TOTAL_CAPITAL * MAX_PER_STOCK_PCT * LEVERAGE

    brokerage = costs["brokerage"]
    stt_buy   = costs["stt_buy"]
    stt_sell  = costs["stt_sell"]

    daily_records = []
    trade_records = []

    # i goes from 2 to len-2: need 2 prior days AND 1 next day
    for i in range(2, len(trading_dates) - 1):
        signal_date = trading_dates[i - 1]   # yesterday
        prev_date   = trading_dates[i - 2]   # day before yesterday
        entry_date  = trading_dates[i]        # today  → entry at close
        exit_date   = trading_dates[i + 1]   # tomorrow → exit at close

        close_yesterday  = daily_closes.loc[signal_date]
        close_day_before = daily_closes.loc[prev_date]
        close_today      = daily_closes.loc[entry_date]   # entry price
        close_tomorrow   = daily_closes.loc[exit_date]    # exit price

        # Valid symbols: price data present on all 4 dates
        valid_mask = (
            close_yesterday.notna()  &
            close_day_before.notna() &
            close_today.notna()      &
            close_tomorrow.notna()   &
            (close_day_before > 0)   &
            (close_today > 0)        &
            (close_tomorrow > 0)
        )
        valid_syms = close_yesterday[valid_mask].index
        if len(valid_syms) < 2 * N:
            continue

        # ── Signal ────────────────────────────────────────────────────────────
        ret        = ((close_yesterday[valid_syms] - close_day_before[valid_syms])
                      / close_day_before[valid_syms])
        market_avg = ret.mean()
        deviation  = ret - market_avg

        filtered   = deviation[deviation.abs() >= MIN_DEVIATION]
        n_eligible = len(filtered)
        if n_eligible < 2 * N:
            continue

        sorted_dev = filtered.sort_values()
        longs      = sorted_dev.iloc[:N]
        shorts     = sorted_dev.iloc[-N:]
        positions  = pd.concat([longs, shorts])

        # ── Execute ───────────────────────────────────────────────────────────
        day_net_pnl = 0.0
        day_trades  = 0

        for sym, dev in positions.items():
            weight       = -dev / n_eligible
            raw_position = weight * TOTAL_CAPITAL * LEVERAGE
            capped       = float(np.clip(raw_position, -max_per_stock, max_per_stock))

            entry_price = float(close_today[sym])
            exit_price  = float(close_tomorrow[sym])

            quantity = int(abs(capped) / entry_price)
            if quantity == 0:
                continue
            if quantity * entry_price > TOTAL_CAPITAL * LEVERAGE:
                continue

            is_long  = dev < 0
            tv_entry = quantity * entry_price
            tv_exit  = quantity * exit_price

            if is_long:
                gross_pnl = (exit_price - entry_price) * quantity
                # Long: buy at entry, sell at exit
                cost = (tv_entry * brokerage + tv_entry * stt_buy +
                        tv_exit  * brokerage + tv_exit  * stt_sell)
            else:
                gross_pnl = (entry_price - exit_price) * quantity
                # Short: sell at entry, buy at exit
                cost = (tv_entry * brokerage + tv_entry * stt_sell +
                        tv_exit  * brokerage + tv_exit  * stt_buy)

            net_pnl     = gross_pnl - cost
            day_net_pnl += net_pnl
            day_trades  += 1

            trade_records.append({
                "entry_date":  entry_date.date(),
                "exit_date":   exit_date.date(),
                "symbol":      sym,
                "direction":   "LONG" if is_long else "SHORT",
                "deviation":   round(dev, 6),
                "quantity":    quantity,
                "entry_price": round(entry_price, 4),
                "exit_price":  round(exit_price, 4),
                "gross_pnl":   round(gross_pnl, 2),
                "cost":        round(cost, 2),
                "net_pnl":     round(net_pnl, 2),
            })

        daily_records.append({
            "date":    entry_date.date(),
            "trades":  day_trades,
            "pnl_net": round(day_net_pnl, 2),
        })

    return pd.DataFrame(daily_records), pd.DataFrame(trade_records)


# ─────────────────────────────────────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(daily_df: pd.DataFrame, trade_df: pd.DataFrame) -> tuple:
    daily_df = daily_df.copy()
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
# PLOT  (two equity curves on same chart)
# ─────────────────────────────────────────────────────────────────────────────

def save_plot(scenario_results: list):
    colors = ["#1565C0", "#C62828"]
    fig, axes = plt.subplots(2, 1, figsize=(14, 8),
                             gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
    ax1, ax2 = axes

    for (label, daily_df, m), color in zip(scenario_results, colors):
        dates = pd.to_datetime(daily_df["date"])
        ax1.plot(dates, daily_df["equity"], label=f"{label}  (Sharpe {m['sharpe']:.2f})",
                 color=color, linewidth=1.5)
        ax2.plot(dates, daily_df["drawdown_pct"], color=color, linewidth=1.0, alpha=0.8)

    ax1.axhline(TOTAL_CAPITAL, color="#9E9E9E", linestyle="--",
                linewidth=0.8, alpha=0.6, label="Starting Capital")
    ax1.set_title(
        "Cross-Sectional Mean Reversion V2 — Overnight Hold\n"
        "Ernie Chan · Quantitative Trading (2008) · Entry: Close → Exit: Next Close",
        fontsize=12, fontweight="bold",
    )
    ax1.set_ylabel("Portfolio Value (₹)", fontsize=10)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x/1e6:.2f}M"))
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
    plt.savefig(EQUITY_CURVE_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"\nEquity curve → {EQUITY_CURVE_PATH}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("Cross-Sectional Mean Reversion V2 — Overnight Hold")
    print(f"Capital: ₹{TOTAL_CAPITAL:,}  |  N={N}  |  Min dev: {MIN_DEVIATION*100:.1f}%")
    print("Entry: today's CLOSE  |  Exit: tomorrow's CLOSE")
    print("=" * 65)

    print("""
⚠️  INDIA MARKET STRUCTURE NOTE:
   Cash equity shorts MUST be squared intraday (SEBI rule).
   Overnight shorts require equity FUTURES (F&O), which have
   different margin requirements and different cost structure.
   This backtest treats both legs at the specified cost rate
   as a signal-quality check. See cost comparison below.
""")

    loader      = DataLoader()
    daily_closes = load_daily_closes(loader)

    scenario_results = []

    for label, costs in COST_SCENARIOS.items():
        print(f"Running with {label}...")
        daily_df, trade_df = simulate(daily_closes, costs)

        if daily_df.empty:
            print(f"  → No trades. Skipping.\n")
            continue

        daily_df, m = compute_metrics(daily_df, trade_df)
        scenario_results.append((label, daily_df, m))

        print(f"\n{'=' * 50}")
        print(f"=== V2 RESULTS — {label} ===")
        print(f"{'=' * 50}")
        print(f"Sharpe Ratio      : {m['sharpe']:.3f}")
        print(f"Max Drawdown %    : {m['max_dd_pct']:.2f}%")
        print(f"Max DD Duration   : {m['max_dd_days']} days")
        print(f"Total Trades      : {m['total_trades']:,}")
        print(f"Win Rate %        : {m['win_rate']:.1f}%")
        print(f"Net P&L           : ₹{m['net_pnl']:,.0f}")
        print(f"Avg Daily P&L     : ₹{m['avg_daily']:,.0f}")
        print(f"Total Days Tested : {m['total_days']}")
        print()

    # ── Side-by-side summary ──────────────────────────────────────────────────
    print(f"{'=' * 65}")
    print("=== COST SCENARIO COMPARISON ===")
    print(f"{'=' * 65}")
    print(f"{'Metric':<25} {'Intraday 0.085%':>20} {'Delivery 0.26%':>18}")
    print("-" * 65)
    if len(scenario_results) == 2:
        labels = [r[0] for r in scenario_results]
        metrics = [r[2] for r in scenario_results]
        rows = [
            ("Sharpe Ratio",     f"{metrics[0]['sharpe']:.3f}",         f"{metrics[1]['sharpe']:.3f}"),
            ("Max Drawdown %",   f"{metrics[0]['max_dd_pct']:.2f}%",    f"{metrics[1]['max_dd_pct']:.2f}%"),
            ("Max DD Duration",  f"{metrics[0]['max_dd_days']} days",   f"{metrics[1]['max_dd_days']} days"),
            ("Total Trades",     f"{metrics[0]['total_trades']:,}",      f"{metrics[1]['total_trades']:,}"),
            ("Win Rate %",       f"{metrics[0]['win_rate']:.1f}%",       f"{metrics[1]['win_rate']:.1f}%"),
            ("Net P&L",          f"₹{metrics[0]['net_pnl']:,.0f}",       f"₹{metrics[1]['net_pnl']:,.0f}"),
            ("Avg Daily P&L",    f"₹{metrics[0]['avg_daily']:,.0f}",     f"₹{metrics[1]['avg_daily']:,.0f}"),
        ]
        for row in rows:
            print(f"{row[0]:<25} {row[1]:>20} {row[2]:>18}")

    print(f"{'=' * 65}")
    print("\nVs V1 best (open→close, intraday, N=10, ₹10L): Sharpe 0.827, Net ₹969")

    # ── Save CSV (intraday cost scenario) ─────────────────────────────────────
    if scenario_results:
        scenario_results[0][1].to_csv(DAILY_PNL_PATH, index=False)
        print(f"\nDaily P&L CSV  → {DAILY_PNL_PATH}")

    if scenario_results:
        save_plot(scenario_results)

    print("\nDone.")


if __name__ == "__main__":
    main()
