"""
Run the intraday pivot + Supertrend naked ATM selling backtest.

Usage:
    G:\\fyers_data_pipeline\\.venv\\Scripts\\python.exe backtesting/options_pivot_intraday/run_backtest.py
"""
import sys
from pathlib import Path

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from backtesting.options_credit_spread.data_loader import OptionsDataLoader
from backtesting.options_pivot_intraday.signals import (
    build_5min_bars, add_supertrend_7_3, build_daily_pivots,
)
from backtesting.options_pivot_intraday.strategy import run_backtest, LOT_SIZE

RESULTS_DIR = Path(__file__).resolve().parent / "results"

ST_PERIOD, ST_MULT = 7, 3.0
BROKERAGE_PER_TRADE = 40      # naked single leg: entry + exit = 2 orders x Rs 20
MARGIN_PER_LOT = 200_000      # ASSUMPTION for naked ATM short -- confirm with broker


def main():
    print("Loading spot and building 5-min bars + Supertrend(7,3)...")
    loader = OptionsDataLoader()
    spot = loader.spot_1min_series()
    bars = build_5min_bars(spot)
    bars = add_supertrend_7_3(bars, period=ST_PERIOD, multiplier=ST_MULT)
    dir_col = f"supertrend_dir_{ST_PERIOD}_{ST_MULT}"
    print(f"5-min bars: {len(bars):,} ({bars.index.min()} to {bars.index.max()})")

    pivots = build_daily_pivots(spot)
    print(f"Tradeable days (with a valid previous session): {len(pivots):,}")

    print("Running backtest...")
    st_col = f"supertrend_{ST_PERIOD}_{ST_MULT}"
    trades = run_backtest(loader, bars, pivots, dir_col, st_col)
    if not trades:
        print("No trades generated.")
        return

    df = pd.DataFrame(trades).sort_values("exit_time").reset_index(drop=True)
    df["pnl_net"] = df["pnl_rupees"] - BROKERAGE_PER_TRADE
    df["cum_pnl"] = df["pnl_net"].cumsum()
    df["peak"] = df["cum_pnl"].cummax()
    df["dd"] = df["cum_pnl"] - df["peak"]
    df["hold_min"] = (df["exit_time"] - df["entry_time"]).dt.total_seconds() / 60

    total = df["pnl_net"].sum()
    wins = df.loc[df["pnl_net"] > 0, "pnl_net"].sum()
    losses = df.loc[df["pnl_net"] < 0, "pnl_net"].sum()
    pf = wins / abs(losses) if losses else float("inf")
    n_days = df["date"].nunique()
    years = (df["exit_time"].max() - df["exit_time"].min()).days / 365.25

    print("\n" + "=" * 62)
    print("INTRADAY PIVOT + SUPERTREND — NAKED ATM SELLING (1 lot = 75 qty)")
    print("=" * 62)
    print(f"Period          : {df['exit_time'].min().date()} to {df['exit_time'].max().date()} ({years:.2f} yrs)")
    print(f"Trades          : {len(df):,}  over {n_days:,} trading days ({len(df)/n_days:.2f}/day)")
    print(f"Win rate        : {(df['pnl_net'] > 0).mean() * 100:.1f}%")
    print(f"Profit factor   : {pf:.2f}")
    print(f"Net P&L (1 lot) : Rs {total:,.0f}   (avg Rs {df['pnl_net'].mean():,.0f}/trade)")
    print(f"Best / Worst    : Rs {df['pnl_net'].max():,.0f} / Rs {df['pnl_net'].min():,.0f}")
    print(f"Max drawdown    : Rs {df['dd'].min():,.0f}  ({df['dd'].min()/MARGIN_PER_LOT*100:.1f}% of Rs {MARGIN_PER_LOT:,} margin)")
    print(f"Avg hold        : {df['hold_min'].mean():.0f} min")
    print(f"Brokerage paid  : Rs {BROKERAGE_PER_TRADE * len(df):,}")

    print("\nBy exit reason:")
    print(df.groupby("exit_reason")["pnl_net"].agg(["count", "sum", "mean"]).to_string())
    print("\nBy side:")
    print(df.groupby("side")["pnl_net"].agg(
        count=("count"), total=("sum"), avg=("mean"),
    ).join(df.groupby("side")["pnl_net"].apply(lambda s: (s > 0).mean() * 100).rename("win%")).to_string())
    print("\nBy entry number:")
    print(df.groupby("entry_no")["pnl_net"].agg(["count", "sum", "mean"]).to_string())
    print("\nBy year:")
    yr = df.groupby(df["exit_time"].dt.year)["pnl_net"].agg(["count", "sum", "mean"])
    yr["win%"] = df.groupby(df["exit_time"].dt.year)["pnl_net"].apply(lambda s: (s > 0).mean() * 100)
    print(yr.to_string())
    n_approx = int(df["exit_approx"].sum())
    if n_approx:
        print(f"\nExits priced by intrinsic fallback (strike off-grid): {n_approx}")

    # ── chart ────────────────────────────────────────────────────────────
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 9), sharex=True,
                                   gridspec_kw={"height_ratios": [2.2, 1]})
    x = df["exit_time"]
    ax1.plot(x, df["cum_pnl"], color="#1f7a3d", linewidth=1.5)
    ax1.axhline(0, color="#888", linestyle="--", linewidth=1)
    ax1.fill_between(x, 0, df["cum_pnl"], where=df["cum_pnl"] >= 0, color="#1f7a3d", alpha=0.08)
    ax1.fill_between(x, 0, df["cum_pnl"], where=df["cum_pnl"] < 0, color="#c0392b", alpha=0.10)
    ax1.set_title("Intraday Pivot + Supertrend(7,3) — Naked ATM Selling, cumulative P&L (1 lot)",
                  fontsize=12, fontweight="bold")
    ax1.set_ylabel("Cumulative P&L (Rs)")
    ax1.grid(alpha=0.25)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v/1000:.0f}k"))
    ax1.text(0.985, 0.05,
             f"Net: Rs {total:,.0f}   PF {pf:.2f}\nTrades {len(df):,}   Win {(df['pnl_net']>0).mean()*100:.0f}%\n"
             f"Max DD Rs {df['dd'].min():,.0f}",
             transform=ax1.transAxes, ha="right", va="bottom", fontsize=9,
             bbox=dict(boxstyle="round,pad=0.5", facecolor="white", edgecolor="#ccc", alpha=0.9))

    ax2.fill_between(x, df["dd"], 0, color="#c0392b", alpha=0.35)
    ax2.plot(x, df["dd"], color="#c0392b", linewidth=1.0)
    ax2.set_title("Drawdown from cumulative-P&L peak (Rs, 1 lot)", fontsize=11)
    ax2.set_ylabel("Drawdown (Rs)")
    ax2.set_xlabel("Date")
    ax2.grid(alpha=0.25)
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v/1000:.0f}k"))

    fig.tight_layout()
    out_png = RESULTS_DIR / "equity_drawdown.png"
    fig.savefig(out_png, dpi=130)
    out_csv = RESULTS_DIR / "trades.csv"
    df.drop(columns=["peak"]).to_csv(out_csv, index=False)
    print(f"\nSaved: {out_png}\nSaved: {out_csv}")


if __name__ == "__main__":
    main()
