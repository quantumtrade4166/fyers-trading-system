# ============================================================
# backtesting/book_strategies/ernie_chan_qt/crosssectional_v1.py
#
# Cross-Sectional Mean Reversion — Capital Weighted (V1)
# Source: Ernie Chan, "Quantitative Trading" (2008)
#
# Strategy:
#   Each day, rank all stocks by how much their prior-day
#   return deviated from the equal-weighted market average.
#   Go LONG the 10 most negative (under-performers).
#   Go SHORT the 10 most positive (over-performers).
#   Enter at today's open, exit at 15:25 (proxy: daily close).
#
# Usage:
#   G:\fyers_data_pipeline\.venv\Scripts\python.exe
#       backtesting\book_strategies\ernie_chan_qt\crosssectional_v1.py
# ============================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path

# ── Project root on sys.path ──────────────────────────────────────────────────
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
N                 = 5              # top-N longs + top-N shorts = 20 positions
MIN_DEVIATION     = 0.005          # filter: |dev| must be >= 0.5%
MAX_PER_STOCK_PCT = 0.10           # cap: max 10% of capital per stock
LEVERAGE          = 1              # no leverage

# Transaction costs
BROKERAGE_ONEWAY  = 0.0003         # 0.03% per leg
STT_SELL_SIDE     = 0.00025        # 0.025% on sell leg only

# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT PATHS
# ─────────────────────────────────────────────────────────────────────────────
RESULTS_DIR       = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
EQUITY_CURVE_PATH = RESULTS_DIR / "equity_curve_v1.png"
DAILY_PNL_PATH    = RESULTS_DIR / "daily_pnl_v1.csv"


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — LOAD DATA
# ─────────────────────────────────────────────────────────────────────────────

def load_daily_panels(loader: DataLoader):
    """
    Load all 5-min data → resample to daily.
    Returns two aligned DataFrames:
        daily_closes : date × symbol  (previous-day close used for signal)
        daily_opens  : date × symbol  (today's open used for entry price)
    """
    symbols = loader.available_symbols()
    print(f"\nLoading {len(symbols)} symbols...")

    raw_data = loader.load_many(symbols)
    print(f"Loaded: {len(raw_data)} / {len(symbols)} symbols\n")

    print("Resampling to daily OHLCV...")
    closes_dict = {}
    opens_dict  = {}

    for sym, df in raw_data.items():
        try:
            daily = resample_ohlcv(df, "1D")
            if len(daily) < 5:
                continue
            # Normalize index to date only (resample labels at midnight)
            daily.index = daily.index.normalize()
            closes_dict[sym] = daily["close"]
            opens_dict[sym]  = daily["open"]
        except Exception as exc:
            print(f"  Skipped {sym}: {exc}")

    daily_closes = pd.DataFrame(closes_dict).sort_index()
    daily_opens  = pd.DataFrame(opens_dict).sort_index()

    # Align both panels to the same date index
    all_dates    = daily_closes.index.union(daily_opens.index)
    daily_closes = daily_closes.reindex(all_dates)
    daily_opens  = daily_opens.reindex(all_dates)

    print(f"Daily panels ready: {daily_closes.shape[1]} symbols, "
          f"{daily_closes.index[0].date()} → {daily_closes.index[-1].date()}")
    return daily_closes, daily_opens


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — SIMULATE TRADES
# ─────────────────────────────────────────────────────────────────────────────

def simulate(daily_closes: pd.DataFrame, daily_opens: pd.DataFrame):
    """
    Iterate over every trading day.
    Signal: prior-day return deviation from market average.
    Execution: enter at today's open, exit at daily close (≈ 15:25).
    """
    trading_dates = sorted(daily_closes.index)
    max_per_stock = TOTAL_CAPITAL * MAX_PER_STOCK_PCT * LEVERAGE

    daily_records = []
    trade_records = []

    print("Running simulation...")

    for i, trade_date in enumerate(trading_dates):
        # Need 2 prior days for return calculation
        # Return: (close[signal_date] - close[prev_date]) / close[prev_date]
        if i < 2:
            continue

        signal_date = trading_dates[i - 1]   # yesterday
        prev_date   = trading_dates[i - 2]   # day before yesterday

        close_yesterday  = daily_closes.loc[signal_date]
        close_day_before = daily_closes.loc[prev_date]
        open_today       = daily_opens.loc[trade_date]
        close_today      = daily_closes.loc[trade_date]

        # Only keep symbols with valid data for ALL four price points
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
            continue  # not enough stocks to fill 2N positions

        # ── Signal ───────────────────────────────────────────────────────────
        ret = ((close_yesterday[valid_syms] - close_day_before[valid_syms])
               / close_day_before[valid_syms])

        market_avg = ret.mean()
        deviation  = ret - market_avg

        # Filter: only keep stocks with meaningful deviation
        filtered   = deviation[deviation.abs() >= MIN_DEVIATION]
        n_eligible = len(filtered)

        if n_eligible < 2 * N:
            continue

        # ── Select positions ──────────────────────────────────────────────────
        sorted_dev = filtered.sort_values()
        longs      = sorted_dev.iloc[:N]          # most negative → LONG
        shorts     = sorted_dev.iloc[-N:]         # most positive → SHORT
        positions  = pd.concat([longs, shorts])   # combined Series

        # ── Execute trades ────────────────────────────────────────────────────
        day_net_pnl = 0.0
        day_trades  = 0

        for sym, dev in positions.items():
            # Weight: -deviation / n_eligible
            #   → positive for longs (dev < 0), negative for shorts (dev > 0)
            weight       = -dev / n_eligible
            raw_position = weight * TOTAL_CAPITAL * LEVERAGE
            capped       = float(np.clip(raw_position, -max_per_stock, max_per_stock))

            entry_price = float(open_today[sym])
            exit_price  = float(close_today[sym])

            quantity = int(abs(capped) / entry_price)
            if quantity == 0:
                continue

            # Sanity check: total exposure must not exceed capital × leverage
            if quantity * entry_price > TOTAL_CAPITAL * LEVERAGE:
                continue

            is_long = dev < 0   # negative deviation → LONG

            trade_val_entry = quantity * entry_price
            trade_val_exit  = quantity * exit_price

            if is_long:
                # Buy at entry, sell at exit
                gross_pnl = (exit_price - entry_price) * quantity
                stt       = trade_val_exit * STT_SELL_SIDE      # STT on sell (exit)
            else:
                # Sell at entry, buy at exit
                gross_pnl = (entry_price - exit_price) * quantity
                stt       = trade_val_entry * STT_SELL_SIDE     # STT on sell (entry)

            brokerage = (trade_val_entry + trade_val_exit) * BROKERAGE_ONEWAY
            cost      = brokerage + stt
            net_pnl   = gross_pnl - cost

            day_net_pnl += net_pnl
            day_trades  += 1

            trade_records.append({
                "date":        trade_date.date(),
                "symbol":      sym,
                "direction":   "LONG" if is_long else "SHORT",
                "deviation":   round(dev, 6),
                "weight":      round(weight, 6),
                "n_eligible":  n_eligible,
                "quantity":    quantity,
                "entry_price": round(entry_price, 4),
                "exit_price":  round(exit_price, 4),
                "gross_pnl":   round(gross_pnl, 2),
                "cost":        round(cost, 2),
                "net_pnl":     round(net_pnl, 2),
            })

        daily_records.append({
            "date":      trade_date.date(),
            "trades":    day_trades,
            "pnl_net":   round(day_net_pnl, 2),
        })

    daily_df = pd.DataFrame(daily_records)
    trade_df = pd.DataFrame(trade_records)

    print(f"Simulation complete: {len(daily_df)} trading days, "
          f"{len(trade_df)} total trades")
    return daily_df, trade_df


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — METRICS
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(daily_df: pd.DataFrame, trade_df: pd.DataFrame):
    """Compute Sharpe, max drawdown, win rate, and other summary stats."""
    daily_df = daily_df.copy()

    # Cumulative equity
    daily_df["equity"]    = TOTAL_CAPITAL + daily_df["pnl_net"].cumsum()
    daily_df["daily_ret"] = daily_df["pnl_net"] / TOTAL_CAPITAL

    # Sharpe ratio (annualised, 252 trading days)
    mean_ret = daily_df["daily_ret"].mean()
    std_ret  = daily_df["daily_ret"].std()
    sharpe   = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0.0

    # Drawdown
    equity      = daily_df["equity"]
    rolling_max = equity.cummax()
    drawdown    = (equity - rolling_max) / rolling_max * 100
    max_dd_pct  = drawdown.min()
    daily_df["drawdown_pct"] = drawdown

    # Max drawdown duration (consecutive days below peak)
    max_dd_days     = 0
    current_dd_days = 0
    for in_dd in (drawdown < 0):
        if in_dd:
            current_dd_days += 1
            max_dd_days      = max(max_dd_days, current_dd_days)
        else:
            current_dd_days  = 0

    # Trade-level stats
    total_trades = len(trade_df)
    win_rate     = (trade_df["net_pnl"] > 0).mean() * 100 if total_trades else 0.0
    net_pnl      = daily_df["pnl_net"].sum()
    avg_daily    = daily_df["pnl_net"].mean()
    total_days   = len(daily_df)

    metrics = dict(
        sharpe      = sharpe,
        max_dd_pct  = max_dd_pct,
        max_dd_days = max_dd_days,
        total_trades= total_trades,
        win_rate    = win_rate,
        net_pnl     = net_pnl,
        avg_daily   = avg_daily,
        total_days  = total_days,
    )
    return daily_df, metrics


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — PRINT & SAVE
# ─────────────────────────────────────────────────────────────────────────────

def print_results(m: dict):
    print(f"\n{'=' * 45}")
    print(f"=== V1 RESULTS ===")
    print(f"{'=' * 45}")
    print(f"Sharpe Ratio      : {m['sharpe']:.3f}")
    print(f"Max Drawdown %    : {m['max_dd_pct']:.2f}%")
    print(f"Max DD Duration   : {m['max_dd_days']} days")
    print(f"Total Trades      : {m['total_trades']:,}")
    print(f"Win Rate %        : {m['win_rate']:.1f}%")
    print(f"Net P&L           : ₹{m['net_pnl']:,.0f}")
    print(f"Avg Daily P&L     : ₹{m['avg_daily']:,.0f}")
    print(f"Total Days Tested : {m['total_days']}")
    print(f"{'=' * 45}")


def save_csv(daily_df: pd.DataFrame):
    daily_df.to_csv(DAILY_PNL_PATH, index=False)
    print(f"\nDaily P&L CSV  → {DAILY_PNL_PATH}")


def save_equity_curve(daily_df: pd.DataFrame, m: dict):
    dates    = pd.to_datetime(daily_df["date"])
    equity   = daily_df["equity"]
    drawdown = daily_df["drawdown_pct"]

    fig, axes = plt.subplots(
        2, 1, figsize=(14, 8),
        gridspec_kw={"height_ratios": [3, 1]},
        sharex=True,
    )

    # ── Top panel: equity curve ───────────────────────────────────────────────
    ax1 = axes[0]
    ax1.plot(dates, equity, color="#1565C0", linewidth=1.5, label="Portfolio Value")
    ax1.axhline(TOTAL_CAPITAL, color="#9E9E9E", linestyle="--",
                linewidth=0.8, alpha=0.7, label="Starting Capital")
    ax1.set_title(
        "Cross-Sectional Mean Reversion V1 — Equity Curve\n"
        "Ernie Chan · Quantitative Trading (2008) · Capital Weighted",
        fontsize=12, fontweight="bold", pad=10,
    )
    ax1.set_ylabel("Portfolio Value (₹)", fontsize=10)
    ax1.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f"₹{x/1e6:.2f}M")
    )
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(True, alpha=0.25)

    # Stats box
    stats_text = (
        f"Sharpe: {m['sharpe']:.2f}  |  "
        f"Max DD: {m['max_dd_pct']:.1f}%  |  "
        f"Net P&L: ₹{m['net_pnl']:,.0f}  |  "
        f"Win Rate: {m['win_rate']:.1f}%  |  "
        f"Trades: {m['total_trades']:,}"
    )
    ax1.text(
        0.01, 0.97, stats_text,
        transform=ax1.transAxes, fontsize=8.5,
        verticalalignment="top",
        bbox=dict(boxstyle="round,pad=0.4", facecolor="#FFF9C4", alpha=0.85),
    )

    # ── Bottom panel: drawdown ────────────────────────────────────────────────
    ax2 = axes[1]
    ax2.fill_between(dates, drawdown, 0, color="#E53935", alpha=0.55, label="Drawdown")
    ax2.set_ylabel("Drawdown %", fontsize=9)
    ax2.set_xlabel("Date", fontsize=9)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax2.grid(True, alpha=0.25)
    ax2.legend(loc="lower left", fontsize=9)

    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    plt.savefig(EQUITY_CURVE_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Equity curve plot → {EQUITY_CURVE_PATH}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Cross-Sectional Mean Reversion V1 — Ernie Chan QT")
    print(f"Capital: ₹{TOTAL_CAPITAL:,}  |  N={N}  |  Leverage={LEVERAGE}")
    print(f"Min deviation: {MIN_DEVIATION*100:.1f}%  |  Max per stock: {MAX_PER_STOCK_PCT*100:.0f}%")
    print(f"Costs: {BROKERAGE_ONEWAY*100:.3f}% brokerage/leg + {STT_SELL_SIDE*100:.3f}% STT")
    print("=" * 60)

    loader = DataLoader()

    # Load and resample
    daily_closes, daily_opens = load_daily_panels(loader)

    # Simulate
    daily_df, trade_df = simulate(daily_closes, daily_opens)

    if daily_df.empty or trade_df.empty:
        print("\nNo trades generated — check data and parameters.")
        return

    # Metrics
    daily_df, metrics = compute_metrics(daily_df, trade_df)

    # Output
    print_results(metrics)
    save_csv(daily_df)
    save_equity_curve(daily_df, metrics)

    print("\nDone.")


if __name__ == "__main__":
    main()
