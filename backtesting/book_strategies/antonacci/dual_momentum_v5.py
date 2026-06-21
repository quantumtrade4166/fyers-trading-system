"""
Dual Momentum V5 — Nifty 100MA filter + liquid fund returns on cash (6% p.a.)
Compares:
  A) 100MA, cash earns 0%   (V4 baseline)
  B) 100MA, cash earns 6%   (liquid fund)
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf

DATA_DIR        = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
LOOKBACK_DAYS   = 252
TOP_N           = 50
CAPITAL         = 1_000_000
SLIPPAGE_PCT    = 0.001
LIQUID_FUND_PA  = 0.06       # 6% per annum on cash
START_DATE      = "2006-01-01"
END_DATE        = "2026-06-18"

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
monthly_ends = prices.resample("ME").last().index
print(f"Price matrix: {prices.shape[0]} days x {prices.shape[1]} symbols\n")


def run_backtest(cash_rate, label):
    # monthly compounding rate
    monthly_rate = (1 + cash_rate) ** (1/12) - 1

    portfolio_value = [CAPITAL]
    dates           = [monthly_ends[0]]
    cash_value      = CAPITAL
    held_stocks     = {}
    cash_months     = 0
    invested_months = 0
    annual_data     = {}

    prev_date = monthly_ends[0]

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date = prices.index[idx]
        current_px = prices.iloc[idx]

        # days elapsed since last rebalance (for liquid fund accrual)
        days_elapsed = (rebal_date - prev_date).days

        # mark-to-market stocks
        nav = 0
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                nav += shares * p

        # accrue liquid fund on cash portion
        if cash_rate > 0:
            months_elapsed = days_elapsed / 30.44
            cash_accrued = cash_value * ((1 + monthly_rate) ** months_elapsed - 1)
            cash_value += cash_accrued

        nav += cash_value

        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            portfolio_value.append(nav)
            dates.append(rebal_date)
            prev_date = rebal_date
            continue

        # absolute filter: Nifty vs 100MA
        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        n_ma = nifty_ma100.iloc[nifty_idx]
        n_px = nifty.iloc[nifty_idx]
        market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        if not market_up:
            cash_months += 1
        else:
            invested_months += 1

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px - 1).dropna()
        candidates  = returns_12m.nlargest(TOP_N).index.tolist() if market_up else []

        # sell everything
        sell_value = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_value += shares * p * (1 - SLIPPAGE_PCT)

        # buy candidates
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

        yr = rebal_date.year
        annual_data[yr] = nav

        prev_date = rebal_date

    # final NAV
    final_nav = cash_value
    if cash_rate > 0 and not held_stocks:
        pass  # already accrued above
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
    total_m   = cash_months + invested_months

    return {
        "label":           label,
        "cagr":            cagr,
        "sharpe":          sharpe,
        "max_dd":          max_dd,
        "final_nav":       nav_s.iloc[-1],
        "cash_months":     cash_months,
        "invested_months": invested_months,
        "total_months":    total_m,
        "nav_series":      nav_s,
    }


configs = [
    (0.00, "A) 100MA, cash = 0%"),
    (0.06, "B) 100MA, cash = 6% (liquid fund)"),
]

results = []
for rate, label in configs:
    print(f"  Running {label}...", end=" ", flush=True)
    r = run_backtest(rate, label)
    results.append(r)
    print(f"CAGR={r['cagr']*100:.2f}%  Sharpe={r['sharpe']:.3f}  MaxDD={r['max_dd']*100:.1f}%  Cash={r['cash_months']}m/{r['total_months']}m")

print("\n" + "=" * 72)
print("  DUAL MOMENTUM V5 — LIQUID FUND ON CASH COMPARISON (TOP_N=50)")
print("=" * 72)
print(f"  {'Config':<35} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'FinalNAV':>14}")
print(f"  {'-'*35} {'-'*7} {'-'*8} {'-'*8} {'-'*14}")
for r in results:
    print(f"  {r['label']:<35} {r['cagr']*100:>6.2f}% {r['sharpe']:>8.3f} {r['max_dd']*100:>7.1f}% {r['final_nav']:>14,.0f}")
print("=" * 72)

boost = results[1]["cagr"] - results[0]["cagr"]
print(f"\n  Liquid fund boost: +{boost*100:.2f}% CAGR")
print(f"  Cash months: {results[0]['cash_months']} / {results[0]['total_months']} ({results[0]['cash_months']/results[0]['total_months']*100:.0f}% of time)")
print(f"  Liquid fund contribution: {results[0]['cash_months']/results[0]['total_months']*100:.0f}% of time × 6% = ~{results[0]['cash_months']/results[0]['total_months']*6:.1f}% effective boost")

print("\n  Year-by-year:")
print(f"  {'Year':<6} {'Cash 0%':>10} {'Cash 6%':>10} {'Boost':>8}")
print(f"  {'----':<6} {'-------':>10} {'-------':>10} {'-----':>8}")
navs = [r["nav_series"].resample("YE").last() for r in results]
all_years = sorted(set().union(*[set(n.index.year) for n in navs]))
for yr in all_years:
    row_vals = []
    for nav in navs:
        yr_idx = [i for i, d in enumerate(nav.index) if d.year == yr]
        if yr_idx and yr_idx[0] > 0:
            ret = (nav.iloc[yr_idx[0]] / nav.iloc[yr_idx[0] - 1]) - 1
            row_vals.append(ret)
        else:
            row_vals.append(None)
    if all(v is not None for v in row_vals):
        boost_yr = row_vals[1] - row_vals[0]
        print(f"  {yr:<6} {row_vals[0]*100:>9.1f}% {row_vals[1]*100:>9.1f}% {boost_yr*100:>+7.1f}%")

out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
pd.DataFrame({r["label"][:1]: r["nav_series"] for r in results}).to_csv(out_dir / "dual_momentum_v5_nav.csv")
print(f"\n  NAV saved → {out_dir / 'dual_momentum_v5_nav.csv'}")
