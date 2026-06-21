"""
Dual Momentum — Gary Antonacci (Dual Momentum Investing, 2014)
Applied to Nifty 500 universe, daily data 2005-2026.

Rules (monthly rebalance):
  1. Rank all 500 stocks by 12-month trailing return (relative momentum)
  2. Pick top N stocks
  3. Absolute filter: only hold if 12-month return > ABS_HURDLE (annualised)
     — stocks that don't pass go to "cash" (earn 0% — conservative)
  4. Equal-weight the held positions
  5. Rebalance on the last trading day of each month
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import pandas as pd
import numpy as np
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR      = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
LOOKBACK_DAYS = 252          # 12-month return window
TOP_N         = 20           # number of stocks to hold
ABS_HURDLE    = 0.06         # 6% annualised — stocks below go to cash
CAPITAL       = 1_000_000   # Rs 10L starting capital
SLIPPAGE_PCT  = 0.001        # 0.1% round-trip per trade (conservative)
START_DATE    = "2006-01-01" # need 12 months of history before first signal
END_DATE      = "2026-06-18"

# ── Load all symbols ───────────────────────────────────────────────────────────
print("Loading 500 symbols...")
frames = {}
for f in DATA_DIR.glob("*.parquet"):
    sym = f.stem
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[sym] = df["close"]

prices = pd.DataFrame(frames).sort_index()
prices = prices.loc[START_DATE:END_DATE]
print(f"Price matrix: {prices.shape[0]} days x {prices.shape[1]} symbols")
print(f"Date range: {prices.index[0].date()} to {prices.index[-1].date()}")

# ── Monthly rebalance dates (last trading day of each month) ───────────────────
monthly_ends = prices.resample("ME").last().index

# ── Backtest loop ──────────────────────────────────────────────────────────────
portfolio_value = [CAPITAL]
dates           = [monthly_ends[0]]
holdings_log    = []

cash_value  = CAPITAL
held_stocks = {}  # sym -> shares

print(f"\nRunning backtest: TOP_N={TOP_N}, LOOKBACK={LOOKBACK_DAYS}d, ABS_HURDLE={ABS_HURDLE*100:.0f}%\n")

for i, rebal_date in enumerate(monthly_ends[1:], 1):
    prev_date = monthly_ends[i - 1]

    # Current prices at rebalance date
    if rebal_date not in prices.index:
        rebal_date = prices.index[prices.index.get_indexer([rebal_date], method="ffill")[0]]

    current_prices = prices.loc[rebal_date]

    # ── Mark-to-market existing holdings ─────────────────────────────────────
    portfolio_nav = cash_value
    for sym, shares in held_stocks.items():
        if sym in current_prices and not pd.isna(current_prices[sym]):
            portfolio_nav += shares * current_prices[sym]

    # ── Compute 12-month returns for all symbols ──────────────────────────────
    lookback_date_idx = prices.index.get_indexer([rebal_date], method="ffill")[0] - LOOKBACK_DAYS
    if lookback_date_idx < 0:
        portfolio_value.append(portfolio_nav)
        dates.append(rebal_date)
        continue

    lookback_date = prices.index[lookback_date_idx]
    past_prices   = prices.loc[lookback_date]

    returns_12m = (current_prices / past_prices) - 1
    returns_12m = returns_12m.dropna()

    # ── Relative momentum: top N ──────────────────────────────────────────────
    ranked = returns_12m.nlargest(TOP_N)

    # ── Absolute momentum filter: must beat hurdle ────────────────────────────
    candidates = ranked[ranked >= ABS_HURDLE]

    n_held    = len(candidates)
    n_cash    = TOP_N - n_held

    # ── Sell everything, apply slippage ──────────────────────────────────────
    sell_value = cash_value
    for sym, shares in held_stocks.items():
        if sym in current_prices and not pd.isna(current_prices[sym]):
            gross = shares * current_prices[sym]
            sell_value += gross * (1 - SLIPPAGE_PCT)

    # ── Buy new candidates equally ────────────────────────────────────────────
    held_stocks = {}
    cash_value  = sell_value  # start fresh

    if n_held > 0:
        per_stock = (cash_value / TOP_N) * n_held / n_held  # equal weight among candidates
        per_stock = cash_value / TOP_N  # each slot = 1/TOP_N of capital

        invested = 0
        for sym in candidates.index:
            price = current_prices.get(sym, np.nan)
            if pd.isna(price) or price <= 0:
                continue
            buy_cost = per_stock * (1 + SLIPPAGE_PCT)
            shares   = buy_cost / price
            held_stocks[sym] = shares
            invested += buy_cost

        cash_value = sell_value - invested
        if cash_value < 0:
            cash_value = 0
    else:
        # All in cash
        pass

    holdings_log.append({
        "date":    rebal_date,
        "held":    n_held,
        "in_cash": n_cash,
        "stocks":  list(candidates.index),
        "nav":     portfolio_nav,
    })

    portfolio_value.append(portfolio_nav)
    dates.append(rebal_date)

# ── Final NAV ─────────────────────────────────────────────────────────────────
final_nav = cash_value
for sym, shares in held_stocks.items():
    last_price = prices[sym].dropna().iloc[-1] if sym in prices else 0
    final_nav += shares * last_price

portfolio_value.append(final_nav)
dates.append(prices.index[-1])

# ── Results ───────────────────────────────────────────────────────────────────
nav_series = pd.Series(portfolio_value, index=dates)
nav_series = nav_series[~nav_series.index.duplicated(keep="last")]

returns_m  = nav_series.pct_change().dropna()

total_return = (nav_series.iloc[-1] / nav_series.iloc[0]) - 1
n_years      = (nav_series.index[-1] - nav_series.index[0]).days / 365.25
cagr         = (nav_series.iloc[-1] / nav_series.iloc[0]) ** (1 / n_years) - 1

rolling_max  = nav_series.cummax()
drawdown     = (nav_series - rolling_max) / rolling_max
max_dd       = drawdown.min()

sharpe       = returns_m.mean() / returns_m.std() * np.sqrt(12) if returns_m.std() > 0 else 0

print("=" * 55)
print("  DUAL MOMENTUM — NIFTY 500 — BACKTEST RESULTS")
print("=" * 55)
print(f"  Period        : {nav_series.index[0].date()} → {nav_series.index[-1].date()}")
print(f"  Universe      : 500 stocks (Nifty 500 daily)")
print(f"  Top N         : {TOP_N} stocks")
print(f"  Abs Hurdle    : {ABS_HURDLE*100:.0f}% annualised")
print(f"  Lookback      : {LOOKBACK_DAYS} trading days (12 months)")
print(f"  Starting Cap  : Rs {CAPITAL:,.0f}")
print(f"  Final NAV     : Rs {final_nav:,.0f}")
print(f"  Total Return  : {total_return*100:.1f}%")
print(f"  CAGR          : {cagr*100:.2f}%")
print(f"  Sharpe Ratio  : {sharpe:.3f}")
print(f"  Max Drawdown  : {max_dd*100:.2f}%")
print("=" * 55)

# Monthly breakdown — how often were we in cash?
in_cash_pct = np.mean([h["in_cash"] for h in holdings_log]) / TOP_N * 100
print(f"\n  Avg cash allocation : {in_cash_pct:.1f}% of portfolio")
print(f"  Total rebalances    : {len(holdings_log)}")

# Year-by-year
print("\n  Year-by-Year Returns:")
print(f"  {'Year':<6} {'Return':>8} {'NAV':>12}")
print(f"  {'----':<6} {'------':>8} {'---':>12}")
annual = nav_series.resample("YE").last()
for i in range(1, len(annual)):
    yr  = annual.index[i].year
    ret = (annual.iloc[i] / annual.iloc[i - 1]) - 1
    print(f"  {yr:<6} {ret*100:>7.1f}% {annual.iloc[i]:>12,.0f}")

# Recent holdings (last rebalance)
if holdings_log:
    last = holdings_log[-1]
    print(f"\n  Last rebalance ({last['date'].date()}): held {last['held']} stocks")
    print(f"  Top holdings: {', '.join(last['stocks'][:10])}")

# Save NAV to CSV
out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
nav_series.to_csv(out_dir / "dual_momentum_nav.csv", header=["nav"])
print(f"\n  NAV saved → {out_dir / 'dual_momentum_nav.csv'}")
