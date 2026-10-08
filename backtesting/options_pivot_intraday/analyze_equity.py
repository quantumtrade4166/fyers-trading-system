"""
Account-level view of the intraday pivot + Supertrend naked ATM strategy.

Assumptions:
  - Capital: Rs 1,50,000 (= margin for 1 naked ATM lot)
  - Fixed 1 lot throughout (no compounding / no lot scaling as equity grows)
  - Brokerage: Rs 20 per order; naked short = 1 leg, so entry + exit
    = 2 orders = Rs 40 per round-trip trade
  - Statutory charges (STT / exchange / GST / stamp) and slippage NOT included
"""
import sys
from pathlib import Path

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

RESULTS_DIR = Path(__file__).resolve().parent / "results"

CAPITAL = 150_000
BROKERAGE_PER_ORDER = 20
ORDERS_PER_TRADE = 2            # naked single leg: 1 entry + 1 exit
BROKERAGE_PER_TRADE = BROKERAGE_PER_ORDER * ORDERS_PER_TRADE


def main():
    df = pd.read_csv(RESULTS_DIR / "trades.csv", parse_dates=["entry_time", "exit_time"])
    df = df.sort_values("exit_time").reset_index(drop=True)

    df["pnl_net"] = df["pnl_rupees"] - BROKERAGE_PER_TRADE
    df["equity"] = CAPITAL + df["pnl_net"].cumsum()
    df["peak"] = df["equity"].cummax()
    df["dd_rupees"] = df["equity"] - df["peak"]
    df["dd_pct"] = df["dd_rupees"] / df["peak"] * 100
    df["year"] = df["exit_time"].dt.year

    net = df["pnl_net"].sum()
    final = df["equity"].iloc[-1]
    start, end = df["exit_time"].min(), df["exit_time"].max()
    years = (end - start).days / 365.25
    cagr = ((final / CAPITAL) ** (1 / years) - 1) * 100
    simple_annual = net / years / CAPITAL * 100
    wins = df.loc[df["pnl_net"] > 0, "pnl_net"].sum()
    losses = df.loc[df["pnl_net"] < 0, "pnl_net"].sum()
    pf = wins / abs(losses)
    max_dd_r, max_dd_p = df["dd_rupees"].min(), df["dd_pct"].min()

    print("=" * 68)
    print("INTRADAY PIVOT + SUPERTREND(7,3) — NAKED ATM SELLING")
    print(f"Capital Rs {CAPITAL:,} | 1 lot (75 qty) fixed | Rs {BROKERAGE_PER_ORDER}/order")
    print("=" * 68)
    print(f"Period            : {start.date()} to {end.date()}  ({years:.2f} years)")
    print(f"Trades            : {len(df):,}")
    print(f"Win rate          : {(df['pnl_net'] > 0).mean() * 100:.1f}%   Profit factor: {pf:.2f}")
    print(f"Starting capital  : Rs {CAPITAL:,}")
    print(f"Final equity      : Rs {final:,.0f}")
    print(f"NET PROFIT        : Rs {net:,.0f}   ({net / CAPITAL * 100:.1f}% on capital)")
    print(f"CAGR              : {cagr:.2f}%   (equity-curve growth, fixed 1 lot — no compounding)")
    print(f"Simple annual ret : {simple_annual:.2f}% of capital per year")
    print(f"Max drawdown      : Rs {max_dd_r:,.0f}  ({max_dd_p:.2f}%)")
    print(f"Brokerage paid    : Rs {BROKERAGE_PER_TRADE * len(df):,}")

    # ── yearly table ─────────────────────────────────────────────────────
    rows = []
    for yr, g in df.groupby("year"):
        start_eq = CAPITAL if yr == df["year"].iloc[0] else df.loc[df["year"] < yr, "equity"].iloc[-1]
        pnl = g["pnl_net"].sum()
        rows.append({
            "Year": yr,
            "Trades": len(g),
            "Win %": round((g["pnl_net"] > 0).mean() * 100, 1),
            "Abs Profit (Rs)": round(pnl),
            "% on Capital (1.5L)": round(pnl / CAPITAL * 100, 1),
            "% on Year-Start Equity": round(pnl / start_eq * 100, 1),
            "Year-End Equity": round(g["equity"].iloc[-1]),
        })
    yearly = pd.DataFrame(rows)
    print("\nYEAR-BY-YEAR")
    print(yearly.to_string(index=False))
    yearly.to_csv(RESULTS_DIR / "yearly_breakdown.csv", index=False)

    # ── chart ────────────────────────────────────────────────────────────
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 9), sharex=True,
                                   gridspec_kw={"height_ratios": [2.2, 1]})
    x = df["exit_time"]

    ax1.plot(x, df["equity"], color="#1f7a3d", linewidth=1.6)
    ax1.axhline(CAPITAL, color="#888", linestyle="--", linewidth=1, label=f"Start capital Rs {CAPITAL:,}")
    ax1.fill_between(x, CAPITAL, df["equity"], where=df["equity"] >= CAPITAL, color="#1f7a3d", alpha=0.08)
    ax1.fill_between(x, CAPITAL, df["equity"], where=df["equity"] < CAPITAL, color="#c0392b", alpha=0.10)
    ax1.set_title("Intraday Pivot + Supertrend(7,3) — Naked ATM Selling\n"
                  f"Rs {CAPITAL:,} capital, 1 lot fixed, Rs {BROKERAGE_PER_ORDER}/order",
                  fontsize=12, fontweight="bold")
    ax1.set_ylabel("Equity (Rs)")
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(alpha=0.25)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v/1000:.0f}k"))
    ax1.text(0.985, 0.05,
             f"Net profit: Rs {net:,.0f}  ({net/CAPITAL*100:.0f}%)\n"
             f"CAGR {cagr:.1f}%   Max DD {max_dd_p:.1f}%\n"
             f"Trades {len(df):,}   Win {(df['pnl_net']>0).mean()*100:.0f}%   PF {pf:.2f}",
             transform=ax1.transAxes, ha="right", va="bottom", fontsize=9,
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
    out = RESULTS_DIR / "equity_drawdown_150k.png"
    fig.savefig(out, dpi=130)
    print(f"\nSaved chart: {out}")
    print(f"Saved yearly: {RESULTS_DIR / 'yearly_breakdown.csv'}")


if __name__ == "__main__":
    main()
