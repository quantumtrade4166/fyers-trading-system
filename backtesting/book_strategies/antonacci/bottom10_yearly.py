"""
Bottom 10 stocks by annual return — for stocks held in IN months each year.
Compared against Nifty 50 annual return.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf

DATA_DIR      = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
LOOKBACK_DAYS = 252
TOP_N         = 50
START_DATE    = "2006-01-01"
END_DATE      = "2026-06-18"

print("Loading data...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty     = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]
prices = pd.DataFrame(frames).sort_index().loc[START_DATE:END_DATE]
monthly_ends = prices.resample("ME").last().index

# build: for each IN month, collect the selected top 50 stocks
# also track which stocks appeared in each calendar year
print("Running signal extraction...")
yearly_held = {}   # year -> set of symbols held in any IN month

for rebal_date in monthly_ends[1:]:
    idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
    if idx < 0: continue
    rebal_date = prices.index[idx]
    lb_idx = idx - LOOKBACK_DAYS
    if lb_idx < 0: continue

    nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
    n_ma      = nifty_ma100.iloc[nifty_idx]
    n_px      = nifty.iloc[nifty_idx]
    if pd.isna(n_ma): continue
    market_up = n_px > n_ma
    if not market_up: continue

    current_px  = prices.iloc[idx]
    past_px     = prices.iloc[lb_idx]
    returns_12m = (current_px / past_px - 1).dropna()
    top50       = returns_12m.nlargest(TOP_N).index.tolist()

    yr = pd.Timestamp(rebal_date).year
    if yr not in yearly_held:
        yearly_held[yr] = set()
    yearly_held[yr].update(top50)

# for each year: compute Jan-to-Dec return for all held stocks
# use first available price in Jan and last available price in Dec
print("Computing annual returns per stock...")

yearly_px = prices.resample("YE").last()   # year-end prices
yearly_px_start = prices.resample("YS").first()  # year-start prices

# Nifty annual returns
nifty_ye  = nifty.resample("YE").last()
nifty_ys  = nifty.resample("YS").first()
nifty_ann = {}
for yr in nifty_ye.index:
    year = yr.year
    ys_idx = [i for i, d in enumerate(nifty_ys.index) if d.year == year]
    ye_idx = [i for i, d in enumerate(nifty_ye.index) if d.year == year]
    if ys_idx and ye_idx:
        sp = nifty_ys.iloc[ys_idx[0]]
        ep = nifty_ye.iloc[ye_idx[0]]
        if sp > 0:
            nifty_ann[year] = (ep / sp - 1) * 100

print("\n" + "=" * 95)
print("  BOTTOM 10 STOCKS HELD IN IN-MONTHS — ANNUAL RETURN vs NIFTY (2007–2026)")
print("  (Stocks: price return Jan→Dec of that year | only stocks selected in top-50 during IN months)")
print("=" * 95)

all_years = sorted(yearly_held.keys())

for yr in all_years:
    syms = list(yearly_held[yr])
    if not syms:
        continue

    # get year-start and year-end prices
    # year-start: first trading day of the year
    yr_prices = prices[prices.index.year == yr]
    if yr_prices.empty:
        continue

    start_px = yr_prices.iloc[0]   # first day of year
    end_px   = yr_prices.iloc[-1]  # last day of year

    # compute annual return for each held stock
    stock_returns = {}
    for sym in syms:
        sp = start_px.get(sym, np.nan)
        ep = end_px.get(sym, np.nan)
        if not pd.isna(sp) and not pd.isna(ep) and sp > 0:
            stock_returns[sym] = (ep / sp - 1) * 100

    if len(stock_returns) < 10:
        continue

    # bottom 10 by annual return
    bottom10 = sorted(stock_returns.items(), key=lambda x: x[1])[:10]
    nifty_ret = nifty_ann.get(yr, np.nan)
    n_in_stocks = len(stock_returns)

    print(f"\n  {yr}  |  Nifty return: {nifty_ret:+.1f}%  |  Stocks held during IN months: {n_in_stocks}")
    print(f"  {'Rank':<6} {'Symbol':<20} {'Annual Return':>14}  {'vs Nifty':>10}")
    print(f"  {'----':<6} {'------':<20} {'-'*14}  {'-'*10}")
    for rank, (sym, ret) in enumerate(bottom10, 1):
        vs_nifty = ret - nifty_ret if not np.isnan(nifty_ret) else np.nan
        vs_str   = f"{vs_nifty:+.1f}%" if not np.isnan(vs_nifty) else "—"
        sym_clean = sym.replace("-EQ", "")
        print(f"  {rank:<6} {sym_clean:<20} {ret:>13.1f}%  {vs_str:>10}")

# also print a compact cross-year summary table
print("\n\n" + "=" * 95)
print("  COMPACT SUMMARY — Worst performer each year vs Nifty")
print("=" * 95)
print(f"  {'Year':<6} {'Worst Stock':<20} {'Worst Return':>13} {'Nifty':>8} {'Gap':>8}  {'# Stocks held'}")
print(f"  {'----':<6} {'----------':<20} {'-'*13} {'-'*8} {'-'*8}  {'-'*14}")
for yr in all_years:
    syms = list(yearly_held.get(yr, []))
    if not syms: continue
    yr_prices = prices[prices.index.year == yr]
    if yr_prices.empty: continue
    start_px = yr_prices.iloc[0]
    end_px   = yr_prices.iloc[-1]
    stock_returns = {}
    for sym in syms:
        sp = start_px.get(sym, np.nan)
        ep = end_px.get(sym, np.nan)
        if not pd.isna(sp) and not pd.isna(ep) and sp > 0:
            stock_returns[sym] = (ep / sp - 1) * 100
    if not stock_returns: continue
    worst_sym, worst_ret = min(stock_returns.items(), key=lambda x: x[1])
    nifty_ret = nifty_ann.get(yr, np.nan)
    gap = worst_ret - nifty_ret if not np.isnan(nifty_ret) else np.nan
    print(f"  {yr:<6} {worst_sym.replace('-EQ',''):<20} {worst_ret:>12.1f}% {nifty_ret:>7.1f}% {gap:>+7.1f}%  {len(stock_returns)}")
