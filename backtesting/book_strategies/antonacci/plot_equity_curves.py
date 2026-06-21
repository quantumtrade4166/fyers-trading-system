"""
Plot equity curves for Dual Momentum — Baseline vs 100MA vs 200MA
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path
import yfinance as yf

DATA_DIR      = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
LOOKBACK_DAYS = 252
TOP_N         = 50
CAPITAL       = 1_000_000
SLIPPAGE_PCT  = 0.001
START_DATE    = "2006-01-01"
END_DATE      = "2026-06-18"

print("Downloading Nifty 50 (^NSEI)...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()
nifty_ma200 = nifty.rolling(200).mean()

print("Loading 500 symbols...")
frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]

prices = pd.DataFrame(frames).sort_index()
prices = prices.loc[START_DATE:END_DATE]
monthly_ends = prices.resample("ME").last().index


def run_backtest(ma_series):
    portfolio_value = [CAPITAL]
    dates           = [monthly_ends[0]]
    cash_value      = CAPITAL
    held_stocks     = {}
    cash_periods    = []  # (start, end) for shading

    in_cash     = False
    cash_start  = None

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date = prices.index[idx]
        current_px = prices.iloc[idx]

        nav = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                nav += shares * p

        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            portfolio_value.append(nav)
            dates.append(rebal_date)
            continue

        if ma_series is None:
            nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
            nifty_lb  = nifty_idx - LOOKBACK_DAYS
            market_up = nifty.iloc[nifty_idx] > nifty.iloc[nifty_lb] if nifty_lb >= 0 else True
        else:
            nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
            n_ma = ma_series.iloc[nifty_idx]
            n_px = nifty.iloc[nifty_idx]
            market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        # track cash periods
        if not market_up and not in_cash:
            in_cash    = True
            cash_start = rebal_date
        elif market_up and in_cash:
            in_cash = False
            cash_periods.append((cash_start, rebal_date))

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px - 1).dropna()
        candidates  = returns_12m.nlargest(TOP_N).index.tolist() if market_up else []

        sell_value = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_value += shares * p * (1 - SLIPPAGE_PCT)

        held_stocks = {}
        cash_value  = sell_value
        if candidates:
            per_stock = sell_value / TOP_N
            invested  = 0
            for sym in candidates:
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0:
                    continue
                cost = per_stock * (1 + SLIPPAGE_PCT)
                held_stocks[sym] = cost / p
                invested += cost
            cash_value = max(sell_value - invested, 0)

        portfolio_value.append(nav)
        dates.append(rebal_date)

    if in_cash:
        cash_periods.append((cash_start, prices.index[-1]))

    final_nav = cash_value
    for sym, shares in held_stocks.items():
        if sym in prices.columns:
            final_nav += shares * prices[sym].dropna().iloc[-1]
    portfolio_value.append(final_nav)
    dates.append(prices.index[-1])

    nav_s = pd.Series(portfolio_value, index=dates)
    nav_s = nav_s[~nav_s.index.duplicated(keep="last")]
    return nav_s, cash_periods


print("Running backtests...")
nav_base,  cash_base  = run_backtest(None)
nav_100,   cash_100   = run_backtest(nifty_ma100)
nav_200,   cash_200   = run_backtest(nifty_ma200)

# Nifty buy-and-hold (normalised to same capital)
nifty_bh = (nifty / nifty.iloc[nifty.index.get_indexer([START_DATE], method="ffill")[0]]) * CAPITAL
nifty_bh = nifty_bh.loc[START_DATE:END_DATE]

# ── Drawdown series ────────────────────────────────────────────────────────────
def drawdown(nav):
    return (nav - nav.cummax()) / nav.cummax() * 100

# ── Plot ───────────────────────────────────────────────────────────────────────
fig, axes = plt.subplots(3, 1, figsize=(15, 14),
                         gridspec_kw={"height_ratios": [3, 1.2, 1.2]})
fig.patch.set_facecolor("#0f0f0f")
for ax in axes:
    ax.set_facecolor("#0f0f0f")
    ax.tick_params(colors="#aaaaaa", labelsize=9)
    ax.spines["bottom"].set_color("#333333")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#333333")

COLORS = {
    "baseline": "#888888",
    "100ma":    "#00d4ff",
    "200ma":    "#ff9900",
    "nifty":    "#444444",
    "cash":     "#ff000022",
}

# ── Panel 1: NAV (log scale) ───────────────────────────────────────────────────
ax = axes[0]
ax.semilogy(nav_base.index,  nav_base.values,  color=COLORS["baseline"], linewidth=1.2, label="Baseline (12m return)", alpha=0.7)
ax.semilogy(nav_200.index,   nav_200.values,   color=COLORS["200ma"],   linewidth=1.4, label="Nifty 200MA filter", alpha=0.85)
ax.semilogy(nav_100.index,   nav_100.values,   color=COLORS["100ma"],   linewidth=1.8, label="Nifty 100MA filter")
ax.semilogy(nifty_bh.index,  nifty_bh.values,  color=COLORS["nifty"],   linewidth=1.0, label="Nifty 50 B&H", alpha=0.5, linestyle="--")

# shade cash periods for 100MA
for start, end in cash_100:
    ax.axvspan(start, end, alpha=0.12, color="red", zorder=0)

ax.set_ylabel("Portfolio Value (Rs, log scale)", color="#aaaaaa", fontsize=10)
ax.set_title("Dual Momentum — Nifty 500 Universe | TOP_N=50 | Rs 10L starting capital",
             color="#ffffff", fontsize=13, fontweight="bold", pad=12)
ax.legend(loc="upper left", framealpha=0.15, labelcolor="#dddddd",
          facecolor="#111111", edgecolor="#333333", fontsize=9)
ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x/1e6:.0f}M" if x >= 1e6 else f"₹{x/1e3:.0f}K"))
ax.grid(axis="y", color="#222222", linewidth=0.5)
ax.text(0.01, 0.97, "Red shading = 100MA cash periods", transform=ax.transAxes,
        color="#ff6666", fontsize=8, va="top", alpha=0.8)

# annotate final NAVs
for nav, label, color in [
    (nav_100,  f"100MA: ₹{nav_100.iloc[-1]/1e6:.0f}M",  COLORS["100ma"]),
    (nav_200,  f"200MA: ₹{nav_200.iloc[-1]/1e6:.0f}M",  COLORS["200ma"]),
    (nav_base, f"Base:  ₹{nav_base.iloc[-1]/1e6:.0f}M", COLORS["baseline"]),
]:
    ax.annotate(label, xy=(nav.index[-1], nav.iloc[-1]),
                xytext=(10, 0), textcoords="offset points",
                color=color, fontsize=8, va="center")

# ── Panel 2: Drawdown ──────────────────────────────────────────────────────────
ax2 = axes[1]
dd_base = drawdown(nav_base)
dd_100  = drawdown(nav_100)
dd_200  = drawdown(nav_200)

ax2.fill_between(dd_base.index, dd_base.values, 0, color=COLORS["baseline"], alpha=0.3)
ax2.fill_between(dd_100.index,  dd_100.values,  0, color=COLORS["100ma"],   alpha=0.4)
ax2.plot(dd_100.index,  dd_100.values,  color=COLORS["100ma"],   linewidth=1.2, label=f"100MA  (max {dd_100.min():.1f}%)")
ax2.plot(dd_200.index,  dd_200.values,  color=COLORS["200ma"],   linewidth=1.0, label=f"200MA  (max {dd_200.min():.1f}%)", alpha=0.8)
ax2.plot(dd_base.index, dd_base.values, color=COLORS["baseline"], linewidth=0.8, label=f"Baseline (max {dd_base.min():.1f}%)", alpha=0.6)

ax2.set_ylabel("Drawdown %", color="#aaaaaa", fontsize=10)
ax2.legend(loc="lower left", framealpha=0.15, labelcolor="#dddddd",
           facecolor="#111111", edgecolor="#333333", fontsize=9)
ax2.grid(axis="y", color="#222222", linewidth=0.5)
ax2.axhline(0, color="#444444", linewidth=0.8)

# ── Panel 3: Nifty price with 100MA and 200MA ─────────────────────────────────
ax3 = axes[2]
nifty_plot = nifty.loc[START_DATE:END_DATE]
ma100_plot = nifty_ma100.loc[START_DATE:END_DATE]
ma200_plot = nifty_ma200.loc[START_DATE:END_DATE]

ax3.plot(nifty_plot.index, nifty_plot.values, color="#666666",       linewidth=0.8, label="Nifty 50")
ax3.plot(ma100_plot.index, ma100_plot.values, color=COLORS["100ma"], linewidth=1.2, label="100-day MA")
ax3.plot(ma200_plot.index, ma200_plot.values, color=COLORS["200ma"], linewidth=1.2, label="200-day MA", alpha=0.8)

for start, end in cash_100:
    ax3.axvspan(start, end, alpha=0.15, color="red", zorder=0)

ax3.set_ylabel("Nifty 50 Level", color="#aaaaaa", fontsize=10)
ax3.set_xlabel("Date", color="#aaaaaa", fontsize=10)
ax3.legend(loc="upper left", framealpha=0.15, labelcolor="#dddddd",
           facecolor="#111111", edgecolor="#333333", fontsize=9)
ax3.grid(axis="y", color="#222222", linewidth=0.5)

for ax in axes:
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator(2))

plt.tight_layout(rect=[0, 0, 0.93, 1])

out_path = Path(__file__).parent / "results" / "dual_momentum_equity_curve.png"
plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="#0f0f0f")
print(f"Saved → {out_path}")
plt.show()
