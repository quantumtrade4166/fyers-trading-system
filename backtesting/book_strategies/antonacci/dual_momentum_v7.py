"""
Dual Momentum V7 — Position Sizing Comparison (TOP_N=50, Nifty 100MA, 6% liquid fund)

A) Equal weight
B) Momentum score weighted   (weight ∝ 12m return)
C) Inverse volatility        (weight ∝ 1 / 20d realised vol)
D) Tiered                    (top 10 → 3%, next 20 → 1.5%, next 20 → 0.5%)
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
VOL_DAYS       = 20
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

# pre-compute daily returns and rolling vol for all stocks
print("Computing rolling volatility...")
daily_returns = prices.pct_change()
rolling_vol   = daily_returns.rolling(VOL_DAYS).std()

monthly_ends  = prices.resample("ME").last().index
monthly_rate  = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Price matrix: {prices.shape[0]} days x {prices.shape[1]} symbols\n")


def get_weights(sizing, candidates, returns_12m, vol_now, current_px):
    """Return a dict {sym: weight} that sums to 1.0"""
    n = len(candidates)
    if n == 0:
        return {}

    if sizing == "equal":
        return {s: 1/n for s in candidates}

    elif sizing == "momentum":
        raw = {s: max(returns_12m[s], 0.001) for s in candidates}
        total = sum(raw.values())
        return {s: v/total for s, v in raw.items()}

    elif sizing == "inv_vol":
        raw = {}
        for s in candidates:
            v = vol_now.get(s, np.nan)
            raw[s] = 1/v if (not pd.isna(v) and v > 0) else 1.0
        total = sum(raw.values())
        return {s: v/total for s, v in raw.items()}

    elif sizing == "tiered":
        weights = {}
        for rank, sym in enumerate(candidates):
            if rank < 10:
                weights[sym] = 0.03
            elif rank < 30:
                weights[sym] = 0.015
            else:
                weights[sym] = 0.005
        total = sum(weights.values())
        return {s: v/total for s, v in weights.items()}


def run_backtest(sizing, label):
    portfolio_value = [CAPITAL]
    dates           = [monthly_ends[0]]
    cash_value      = CAPITAL
    held_stocks     = {}   # sym -> shares
    cash_months     = 0
    prev_date       = monthly_ends[0]

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date = prices.index[idx]
        current_px = prices.iloc[idx]

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

        # absolute filter
        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        n_ma      = nifty_ma100.iloc[nifty_idx]
        n_px      = nifty.iloc[nifty_idx]
        market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        if not market_up:
            cash_months += 1

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px - 1).dropna()
        vol_now     = rolling_vol.iloc[idx].to_dict()

        candidates = returns_12m.nlargest(TOP_N).index.tolist() if market_up else []

        # sell everything
        sell_value = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_value += shares * p * (1 - SLIPPAGE_PCT)

        held_stocks = {}
        cash_value  = sell_value

        if candidates:
            weights = get_weights(sizing, candidates, returns_12m, vol_now, current_px)
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

    # final NAV
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
    ("equal",    "A) Equal weight"),
    ("momentum", "B) Momentum weighted"),
    ("inv_vol",  "C) Inverse volatility"),
    ("tiered",   "D) Tiered (10/20/20)"),
]

results = []
for sizing, label in configs:
    print(f"  Running {label}...", end=" ", flush=True)
    r = run_backtest(sizing, label)
    results.append(r)
    print(f"CAGR={r['cagr']*100:.2f}%  Sharpe={r['sharpe']:.3f}  MaxDD={r['max_dd']*100:.1f}%  FinalNAV=Rs {r['final_nav']:,.0f}")

print("\n" + "=" * 78)
print("  DUAL MOMENTUM V7 — POSITION SIZING COMPARISON (TOP_N=50, 100MA, 6% cash)")
print("=" * 78)
print(f"  {'Config':<28} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'FinalNAV':>16} {'CashMths':>9}")
print(f"  {'-'*28} {'-'*7} {'-'*8} {'-'*8} {'-'*16} {'-'*9}")
total_m = results[0]["cash_months"]
for r in results:
    print(f"  {r['label']:<28} {r['cagr']*100:>6.2f}% {r['sharpe']:>8.3f} {r['max_dd']*100:>7.1f}% {r['final_nav']:>16,.0f} {r['cash_months']:>5}m")
print("=" * 78)

# year-by-year
print("\n  Year-by-year returns:")
print(f"  {'Year':<6} {'Equal':>9} {'Momentum':>10} {'InvVol':>9} {'Tiered':>9}")
print(f"  {'----':<6} {'-'*9} {'-'*10} {'-'*9} {'-'*9}")
navs = [r["nav_series"].resample("YE").last() for r in results]
all_years = sorted(set().union(*[set(n.index.year) for n in navs]))
for yr in all_years:
    rets = []
    for nav in navs:
        yr_idx = [i for i, d in enumerate(nav.index) if d.year == yr]
        if yr_idx and yr_idx[0] > 0:
            rets.append((nav.iloc[yr_idx[0]] / nav.iloc[yr_idx[0]-1] - 1) * 100)
        else:
            rets.append(None)
    if all(v is not None for v in rets):
        print(f"  {yr:<6} {rets[0]:>8.1f}% {rets[1]:>9.1f}% {rets[2]:>8.1f}% {rets[3]:>8.1f}%")

out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
pd.DataFrame({r["label"][:1]: r["nav_series"] for r in results}).to_csv(out_dir / "dual_momentum_v7_nav.csv")
print(f"\n  NAV saved → {out_dir / 'dual_momentum_v7_nav.csv'}")
