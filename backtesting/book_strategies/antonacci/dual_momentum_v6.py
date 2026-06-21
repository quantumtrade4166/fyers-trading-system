"""
Dual Momentum V6 — Compounding vs Fixed Rs 10L deployment
Both use Nifty 100MA filter + 6% liquid fund on idle cash.

A) Compounding   — deploy full portfolio value every re-entry
B) Fixed Rs 10L  — always deploy exactly Rs 10L into stocks, rest stays in liquid fund
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
CAPITAL         = 1_000_000   # Rs 10L
SLIPPAGE_PCT    = 0.001
LIQUID_FUND_PA  = 0.06
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
monthly_rate = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Price matrix: {prices.shape[0]} days x {prices.shape[1]} symbols\n")


def run_backtest(fixed_deploy, label):
    """
    fixed_deploy: if True, always invest exactly Rs 10L into stocks.
                  Surplus capital sits in a separate liquid fund account.
    """
    portfolio_value = [CAPITAL]
    dates           = [monthly_ends[0]]

    stock_cash      = CAPITAL   # cash available for stock buying
    surplus_cash    = 0.0       # profits parked in liquid fund (fixed mode only)
    held_stocks     = {}
    cash_months     = 0
    prev_date       = monthly_ends[0]

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date = prices.index[idx]
        current_px = prices.iloc[idx]

        days_elapsed   = (rebal_date - prev_date).days
        months_elapsed = days_elapsed / 30.44

        # accrue liquid fund on idle cash
        stock_cash   *= (1 + monthly_rate) ** months_elapsed
        surplus_cash *= (1 + monthly_rate) ** months_elapsed

        # mark-to-market
        stock_nav = stock_cash
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                stock_nav += shares * p

        total_nav = stock_nav + surplus_cash

        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            portfolio_value.append(total_nav)
            dates.append(rebal_date)
            prev_date = rebal_date
            continue

        # absolute filter
        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        n_ma      = nifty_ma100.iloc[nifty_idx]
        n_px      = nifty.iloc[nifty_idx]
        market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        if not market_up:
            cash_months += 1

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px - 1).dropna()
        candidates  = returns_12m.nlargest(TOP_N).index.tolist() if market_up else []

        # sell all stock positions
        sell_value = stock_cash
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_value += shares * p * (1 - SLIPPAGE_PCT)

        held_stocks = {}

        if fixed_deploy:
            # profits above Rs 10L go to surplus liquid fund
            if sell_value > CAPITAL:
                surplus_cash += (sell_value - CAPITAL)
                stock_cash    = CAPITAL
            else:
                stock_cash = sell_value  # still building up to 10L
        else:
            stock_cash = sell_value

        # buy candidates
        deploy = stock_cash
        if candidates:
            per_stock = deploy / TOP_N
            invested  = 0
            for sym in candidates:
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0:
                    continue
                cost = per_stock * (1 + SLIPPAGE_PCT)
                held_stocks[sym] = cost / p
                invested += cost
            stock_cash = max(deploy - invested, 0)

        portfolio_value.append(total_nav)
        dates.append(rebal_date)
        prev_date = rebal_date

    # final NAV
    final_stock = stock_cash
    for sym, shares in held_stocks.items():
        if sym in prices.columns:
            final_stock += shares * prices[sym].dropna().iloc[-1]
    final_nav = final_stock + surplus_cash
    portfolio_value.append(final_nav)
    dates.append(prices.index[-1])

    nav_s     = pd.Series(portfolio_value, index=dates)
    nav_s     = nav_s[~nav_s.index.duplicated(keep="last")]
    returns_m = nav_s.pct_change().dropna()
    n_years   = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr      = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / n_years) - 1
    sharpe    = returns_m.mean() / returns_m.std() * np.sqrt(12) if returns_m.std() > 0 else 0
    max_dd    = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()

    return {
        "label":       label,
        "cagr":        cagr,
        "sharpe":      sharpe,
        "max_dd":      max_dd,
        "final_nav":   nav_s.iloc[-1],
        "cash_months": cash_months,
        "nav_series":  nav_s,
    }


configs = [
    (False, "A) Compounding (full portfolio)"),
    (True,  "B) Fixed Rs 10L deployment"),
]

results = []
for fixed, label in configs:
    print(f"  Running {label}...", end=" ", flush=True)
    r = run_backtest(fixed, label)
    results.append(r)
    print(f"CAGR={r['cagr']*100:.2f}%  Sharpe={r['sharpe']:.3f}  MaxDD={r['max_dd']*100:.1f}%  FinalNAV=Rs {r['final_nav']:,.0f}")

print("\n" + "=" * 72)
print("  DUAL MOMENTUM V6 — COMPOUNDING vs FIXED Rs 10L (TOP_N=50)")
print("=" * 72)
print(f"  {'Config':<35} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'FinalNAV':>16}")
print(f"  {'-'*35} {'-'*7} {'-'*8} {'-'*8} {'-'*16}")
for r in results:
    print(f"  {r['label']:<35} {r['cagr']*100:>6.2f}% {r['sharpe']:>8.3f} {r['max_dd']*100:>7.1f}% {r['final_nav']:>16,.0f}")
print("=" * 72)

print("\n  Year-by-year NAV comparison:")
print(f"  {'Year':<6} {'Compounding':>14} {'Fixed 10L':>14} {'Compound Ret':>13} {'Fixed Ret':>10}")
print(f"  {'----':<6} {'-'*14} {'-'*14} {'-'*13} {'-'*10}")
navs = [r["nav_series"].resample("YE").last() for r in results]
all_years = sorted(set().union(*[set(n.index.year) for n in navs]))
for yr in all_years:
    vals, rets = [], []
    for nav in navs:
        yr_idx = [i for i, d in enumerate(nav.index) if d.year == yr]
        if yr_idx and yr_idx[0] > 0:
            vals.append(nav.iloc[yr_idx[0]])
            rets.append((nav.iloc[yr_idx[0]] / nav.iloc[yr_idx[0]-1] - 1) * 100)
        else:
            vals.append(None); rets.append(None)
    if all(v is not None for v in vals):
        print(f"  {yr:<6} {vals[0]:>14,.0f} {vals[1]:>14,.0f} {rets[0]:>12.1f}% {rets[1]:>9.1f}%")

out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
pd.DataFrame({r["label"][:1]: r["nav_series"] for r in results}).to_csv(out_dir / "dual_momentum_v6_nav.csv")
print(f"\n  NAV saved → {out_dir / 'dual_momentum_v6_nav.csv'}")
