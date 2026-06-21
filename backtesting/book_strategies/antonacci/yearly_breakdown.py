"""
Yearly breakdown — Equal weight vs Momentum weighted
Shows: Year, Start NAV, End NAV, Profit/Loss, Return%, Max NAV, Max DD%
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf

DATA_DIR       = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
LOOKBACK_DAYS  = 252
TOP_N          = 50
CAPITAL        = 1_000_000
SLIPPAGE_PCT   = 0.001
LIQUID_FUND_PA = 0.06
START_DATE     = "2006-01-01"
END_DATE       = "2026-06-18"

print("Downloading Nifty 50 (^NSEI)...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

print("Loading 500 symbols...")
frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]

prices = pd.DataFrame(frames).sort_index()
prices = prices.loc[START_DATE:END_DATE]
daily_returns = prices.pct_change()
rolling_vol   = daily_returns.rolling(20).std()
monthly_ends  = prices.resample("ME").last().index
monthly_rate  = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Price matrix: {prices.shape[0]} days x {prices.shape[1]} symbols\n")


def run_backtest(sizing):
    portfolio_value = [CAPITAL]
    dates           = [monthly_ends[0]]
    cash_value      = CAPITAL
    held_stocks     = {}
    prev_date       = monthly_ends[0]

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date     = prices.index[idx]
        current_px     = prices.iloc[idx]
        months_elapsed = (rebal_date - prev_date).days / 30.44
        cash_value    *= (1 + monthly_rate) ** months_elapsed

        nav = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                nav += shares * p

        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            portfolio_value.append(nav)
            dates.append(rebal_date)
            prev_date = rebal_date
            continue

        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        n_ma      = nifty_ma100.iloc[nifty_idx]
        n_px      = nifty.iloc[nifty_idx]
        market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

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
            if sizing == "equal":
                weights = {s: 1/len(candidates) for s in candidates}
            else:  # momentum
                raw   = {s: max(returns_12m[s], 0.001) for s in candidates}
                total = sum(raw.values())
                weights = {s: v/total for s, v in raw.items()}

            invested = 0
            for sym, w in weights.items():
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0:
                    continue
                cost = sell_value * w * (1 + SLIPPAGE_PCT)
                held_stocks[sym] = cost / p
                invested += cost
            cash_value = max(sell_value - invested, 0)

        portfolio_value.append(nav)
        dates.append(rebal_date)
        prev_date = rebal_date

    final_nav = cash_value
    for sym, shares in held_stocks.items():
        if sym in prices.columns:
            final_nav += shares * prices[sym].dropna().iloc[-1]
    portfolio_value.append(final_nav)
    dates.append(prices.index[-1])

    nav_s = pd.Series(portfolio_value, index=dates)
    return nav_s[~nav_s.index.duplicated(keep="last")]


nav_eq  = run_backtest("equal")
nav_mom = run_backtest("momentum")


def yearly_stats(nav_s):
    rows = []
    years = sorted(set(nav_s.index.year))
    for yr in years:
        yr_data = nav_s[nav_s.index.year == yr]
        if len(yr_data) < 2:
            continue

        # get start NAV (last value of previous year)
        prev = nav_s[nav_s.index.year == yr - 1]
        start_nav = prev.iloc[-1] if len(prev) > 0 else yr_data.iloc[0]
        end_nav   = yr_data.iloc[-1]
        max_nav   = yr_data.max()
        min_nav   = yr_data.min()

        ret_pct   = (end_nav / start_nav - 1) * 100
        pnl       = end_nav - start_nav

        # intra-year max drawdown from peak
        running_max = yr_data.cummax()
        dd          = ((yr_data - running_max) / running_max * 100).min()

        rows.append({
            "Year":      yr,
            "Start NAV": start_nav,
            "End NAV":   end_nav,
            "P&L (Rs)":  pnl,
            "Return %":  ret_pct,
            "Max NAV":   max_nav,
            "Max DD %":  dd,
        })
    return rows


rows_eq  = yearly_stats(nav_eq)
rows_mom = yearly_stats(nav_mom)

def print_table(rows, label):
    print(f"\n{'='*82}")
    print(f"  {label}")
    print(f"{'='*82}")
    print(f"  {'Year':<6} {'Start NAV':>12} {'End NAV':>12} {'P&L (Rs)':>13} {'Return%':>8} {'Max NAV':>12} {'Max DD%':>8}")
    print(f"  {'-'*6} {'-'*12} {'-'*12} {'-'*13} {'-'*8} {'-'*12} {'-'*8}")
    total_pnl = 0
    for r in rows:
        total_pnl += r["P&L (Rs)"]
        flag = " *" if r["Return %"] < 0 else ""
        print(f"  {r['Year']:<6} {r['Start NAV']:>12,.0f} {r['End NAV']:>12,.0f} {r['P&L (Rs)']:>13,.0f} {r['Return %']:>7.1f}%{flag} {r['Max NAV']:>12,.0f} {r['Max DD %']:>7.1f}%")
    print(f"  {'-'*6} {'-'*12} {'-'*12} {'-'*13}")
    print(f"  {'TOTAL':<6} {'':>12} {rows[-1]['End NAV']:>12,.0f} {total_pnl:>13,.0f}")

    # summary stats
    n_years   = len(rows)
    cagr      = (rows[-1]["End NAV"] / rows[0]["Start NAV"]) ** (1/n_years) - 1
    pos_years = sum(1 for r in rows if r["Return %"] > 0)
    worst_dd  = min(r["Max DD %"] for r in rows)
    best_yr   = max(rows, key=lambda r: r["Return %"])
    worst_yr  = min(rows, key=lambda r: r["Return %"])
    print(f"\n  CAGR          : {cagr*100:.2f}%")
    print(f"  Positive years: {pos_years}/{n_years}")
    print(f"  Best year     : {best_yr['Year']} ({best_yr['Return %']:.1f}%)")
    print(f"  Worst year    : {worst_yr['Year']} ({worst_yr['Return %']:.1f}%)")
    print(f"  Worst intra-yr DD: {worst_dd:.1f}%")

print_table(rows_eq,  "A) EQUAL WEIGHT  (TOP_N=50, Nifty 100MA, 6% liquid fund)")
print_table(rows_mom, "B) MOMENTUM WEIGHTED  (TOP_N=50, Nifty 100MA, 6% liquid fund)")

# side-by-side comparison
print(f"\n{'='*70}")
print("  SIDE-BY-SIDE ANNUAL RETURNS")
print(f"{'='*70}")
print(f"  {'Year':<6} {'Equal Ret%':>11} {'Equal P&L':>13} {'Mom Ret%':>10} {'Mom P&L':>13} {'Winner':>8}")
print(f"  {'-'*6} {'-'*11} {'-'*13} {'-'*10} {'-'*13} {'-'*8}")
for eq, mom in zip(rows_eq, rows_mom):
    winner = "MOM" if mom["Return %"] > eq["Return %"] else "EQ "
    print(f"  {eq['Year']:<6} {eq['Return %']:>10.1f}% {eq['P&L (Rs)']:>13,.0f} {mom['Return %']:>9.1f}% {mom['P&L (Rs)']:>13,.0f} {winner:>8}")
