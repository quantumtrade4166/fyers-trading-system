"""
Scale the roll_forward=True backtest to a real account and plot equity +
drawdown.

Assumptions (as specified):
  - Capital: Rs 6,00,000
  - Margin per 1-lot credit spread: Rs 60,000  -> 10 lots
  - Backtest P&L is per 1 lot (75 qty); scale x10
  - Brokerage: Rs 20 per order. A spread = 2 legs; entry (2) + exit (2) = 4
    orders per round-trip trade = Rs 80/trade (flat, does not scale with lots)
  - Statutory charges (STT / exchange / GST / stamp) NOT included
"""
import sys
from pathlib import Path

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

RESULTS_DIR = Path(__file__).resolve().parent / "results"

CAPITAL = 600_000
LOTS = 10
BROKERAGE_PER_TRADE = 80  # 4 orders x Rs 20


def main():
    df = pd.read_csv(RESULTS_DIR / "trades_roll_forward=True.csv", parse_dates=["entry_time", "exit_time"])
    df = df.sort_values("exit_time").reset_index(drop=True)

    df["pnl_10lot"] = df["pnl_rupees"] * LOTS
    df["pnl_net"] = df["pnl_10lot"] - BROKERAGE_PER_TRADE
    df["equity"] = CAPITAL + df["pnl_net"].cumsum()
    df["peak"] = df["equity"].cummax()
    df["dd_rupees"] = df["equity"] - df["peak"]
    df["dd_pct"] = df["dd_rupees"] / df["peak"] * 100

    # ── metrics ──────────────────────────────────────────────────────────
    total_pnl = df["pnl_net"].sum()
    final_equity = df["equity"].iloc[-1]
    ret_pct = total_pnl / CAPITAL * 100
    start, end = df["exit_time"].min(), df["exit_time"].max()
    years = (end - start).days / 365.25
    cagr = ((final_equity / CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else float("nan")
    max_dd_rupees = df["dd_rupees"].min()
    max_dd_pct = df["dd_pct"].min()
    win_rate = (df["pnl_net"] > 0).mean() * 100
    wins = df.loc[df["pnl_net"] > 0, "pnl_net"].sum()
    losses = df.loc[df["pnl_net"] < 0, "pnl_net"].sum()
    profit_factor = wins / abs(losses) if losses != 0 else float("inf")
    total_brokerage = BROKERAGE_PER_TRADE * len(df)

    print(f"Period: {start.date()} to {end.date()} ({years:.2f} yrs)")
    print(f"Trades: {len(df)} | Win rate: {win_rate:.1f}% | Profit factor: {profit_factor:.2f}")
    print(f"Capital: Rs {CAPITAL:,} | Final equity: Rs {final_equity:,.0f}")
    print(f"Total net P&L: Rs {total_pnl:,.0f} ({ret_pct:.1f}% on capital over {years:.1f} yrs)")
    print(f"CAGR: {cagr:.2f}%")
    print(f"Max drawdown: Rs {max_dd_rupees:,.0f} ({max_dd_pct:.2f}%)")
    print(f"Total brokerage paid: Rs {total_brokerage:,} ({len(df)} trades x Rs {BROKERAGE_PER_TRADE})")
    print(f"Avg net P&L/trade: Rs {df['pnl_net'].mean():,.0f}")

    # ── plot ─────────────────────────────────────────────────────────────
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 9), sharex=True,
                                    gridspec_kw={"height_ratios": [2.2, 1]})

    x = df["exit_time"]
    ax1.plot(x, df["equity"], color="#1f7a3d", linewidth=1.6)
    ax1.axhline(CAPITAL, color="#888", linestyle="--", linewidth=1, label=f"Start capital Rs {CAPITAL:,}")
    ax1.fill_between(x, CAPITAL, df["equity"], where=df["equity"] >= CAPITAL, color="#1f7a3d", alpha=0.08)
    ax1.fill_between(x, CAPITAL, df["equity"], where=df["equity"] < CAPITAL, color="#c0392b", alpha=0.10)
    ax1.set_title("Supertrend 1H Credit Spread — Equity Curve (roll_forward=True, 10 lots, Rs 80/trade brokerage)",
                  fontsize=12, fontweight="bold")
    ax1.set_ylabel("Equity (Rs)")
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(alpha=0.25)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v/1000:.0f}k"))

    txt = (f"Net P&L: Rs {total_pnl:,.0f}  ({ret_pct:.1f}%)\n"
           f"CAGR: {cagr:.1f}%   Max DD: {max_dd_pct:.1f}%\n"
           f"Trades: {len(df)}   Win: {win_rate:.0f}%   PF: {profit_factor:.2f}")
    ax1.text(0.985, 0.05, txt, transform=ax1.transAxes, ha="right", va="bottom", fontsize=9,
             bbox=dict(boxstyle="round,pad=0.5", facecolor="white", edgecolor="#ccc", alpha=0.9))

    ax2.fill_between(x, df["dd_pct"], 0, color="#c0392b", alpha=0.35)
    ax2.plot(x, df["dd_pct"], color="#c0392b", linewidth=1.0)
    ax2.set_title("Drawdown (% from equity peak)", fontsize=11)
    ax2.set_ylabel("Drawdown %")
    ax2.set_xlabel("Date")
    ax2.grid(alpha=0.25)
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    fig.tight_layout()
    out = RESULTS_DIR / "equity_drawdown_roll_forward.png"
    fig.savefig(out, dpi=130)
    print(f"\nSaved chart: {out}")


if __name__ == "__main__":
    main()
