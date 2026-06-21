"""
Dual Momentum V4 — Nifty 100MA vs 200MA vs Baseline (TOP_N=50)
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
print(f"Price matrix: {prices.shape[0]} days x {prices.shape[1]} symbols\n")


def run_backtest(ma_series, label):
    portfolio_value = [CAPITAL]
    dates           = [monthly_ends[0]]
    cash_value      = CAPITAL
    held_stocks     = {}
    cash_months     = 0

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

        # Absolute filter
        if ma_series is None:
            # baseline: Nifty 12m return > 0
            nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
            nifty_lb  = nifty_idx - LOOKBACK_DAYS
            market_up = nifty.iloc[nifty_idx] > nifty.iloc[nifty_lb] if nifty_lb >= 0 else True
        else:
            nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
            n_ma = ma_series.iloc[nifty_idx]
            n_px = nifty.iloc[nifty_idx]
            market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px - 1).dropna()
        candidates  = returns_12m.nlargest(TOP_N).index.tolist() if market_up else []
        if not market_up:
            cash_months += 1

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

    final_nav = cash_value
    for sym, shares in held_stocks.items():
        if sym in prices.columns:
            final_nav += shares * prices[sym].dropna().iloc[-1]
    portfolio_value.append(final_nav)
    dates.append(prices.index[-1])

    nav_s     = pd.Series(portfolio_value, index=dates)
    nav_s     = nav_s[~nav_s.index.duplicated(keep="last")]
    returns_m = nav_s.pct_change().dropna()
    n_years   = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr      = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / n_years) - 1
    sharpe    = returns_m.mean() / returns_m.std() * np.sqrt(12) if returns_m.std() > 0 else 0
    max_dd    = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()

    return {"label": label, "cagr": cagr, "sharpe": sharpe, "max_dd": max_dd,
            "final_nav": nav_s.iloc[-1], "cash_months": cash_months, "nav_series": nav_s}


configs = [
    (None,         "A) Baseline (12m return)"),
    (nifty_ma100,  "B) Nifty 100MA"),
    (nifty_ma200,  "C) Nifty 200MA"),
]

results = []
for ma_series, label in configs:
    print(f"  Running {label}...", end=" ", flush=True)
    r = run_backtest(ma_series, label)
    results.append(r)
    print(f"CAGR={r['cagr']*100:.1f}%  Sharpe={r['sharpe']:.3f}  MaxDD={r['max_dd']*100:.1f}%  Cash={r['cash_months']}m")

print("\n" + "=" * 75)
print("  DUAL MOMENTUM — NIFTY 100MA vs 200MA vs BASELINE")
print("=" * 75)
print(f"  {'Config':<28} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'FinalNAV':>16} {'CashMths':>9}")
print(f"  {'-'*28} {'-'*7} {'-'*8} {'-'*8} {'-'*16} {'-'*9}")
total_m = len(results[0]["nav_series"]) - 2
for r in results:
    print(f"  {r['label']:<28} {r['cagr']*100:>6.1f}% {r['sharpe']:>8.3f} {r['max_dd']*100:>7.1f}% {r['final_nav']:>16,.0f} {r['cash_months']:>5}/{total_m}")
print("=" * 75)

print("\n  Year-by-year returns:")
print(f"  {'Year':<6} {'Baseline':>10} {'Nifty100MA':>12} {'Nifty200MA':>12}")
print(f"  {'----':<6} {'--------':>10} {'----------':>12} {'----------':>12}")
navs = [r["nav_series"].resample("YE").last() for r in results]
all_years = sorted(set().union(*[set(n.index.year) for n in navs]))
for yr in all_years:
    row = f"  {yr:<6}"
    for nav in navs:
        yr_idx = [i for i, d in enumerate(nav.index) if d.year == yr]
        if yr_idx and yr_idx[0] > 0:
            ret = (nav.iloc[yr_idx[0]] / nav.iloc[yr_idx[0] - 1]) - 1
            row += f" {ret*100:>9.1f}%"
        else:
            row += f" {'--':>10}"
    print(row)

out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
pd.DataFrame({r["label"][:1]: r["nav_series"] for r in results}).to_csv(out_dir / "dual_momentum_v4_nav.csv")
print(f"\n  NAV saved → {out_dir / 'dual_momentum_v4_nav.csv'}")
