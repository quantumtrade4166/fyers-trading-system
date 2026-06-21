"""
Dual Momentum V3 — DD reduction tests (TOP_N=50 baseline)
Tests 4 configurations:
  A) Baseline          — Nifty 12m return filter only (V2)
  B) Nifty 200MA       — go to cash if Nifty < 200-day MA
  C) Stock 200MA       — only buy stocks above their own 200-day MA
  D) Both              — Nifty 200MA + Stock 200MA combined
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
MA_DAYS       = 200
TOP_N         = 50
CAPITAL       = 1_000_000
SLIPPAGE_PCT  = 0.001
START_DATE    = "2006-01-01"
END_DATE      = "2026-06-18"

# ── Load Nifty 50 ──────────────────────────────────────────────────────────────
print("Downloading Nifty 50 (^NSEI)...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma200 = nifty.rolling(MA_DAYS).mean()

# ── Load 500 symbols ───────────────────────────────────────────────────────────
print("Loading 500 symbols...")
frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]

prices = pd.DataFrame(frames).sort_index()
prices = prices.loc[START_DATE:END_DATE]

# Pre-compute 200-day MA for all stocks
print("Computing 200-day MAs for all stocks...")
ma200 = prices.rolling(MA_DAYS).mean()

monthly_ends = prices.resample("ME").last().index
print(f"Price matrix: {prices.shape[0]} days x {prices.shape[1]} symbols\n")


def run_backtest(use_nifty_ma, use_stock_ma, label):
    portfolio_value = [CAPITAL]
    dates           = [monthly_ends[0]]
    cash_value      = CAPITAL
    held_stocks     = {}
    cash_months     = 0
    partial_months  = 0

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date = prices.index[idx]
        current_px = prices.iloc[idx]

        # mark-to-market
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

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px) - 1
        returns_12m = returns_12m.dropna()

        # ── Absolute filter: Nifty ─────────────────────────────────────────────
        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]

        if use_nifty_ma:
            # Nifty must be above its 200-day MA
            n_ma = nifty_ma200.iloc[nifty_idx] if nifty_idx >= 0 else np.nan
            n_px = nifty.iloc[nifty_idx] if nifty_idx >= 0 else np.nan
            market_up = (not pd.isna(n_ma)) and (n_px > n_ma)
        else:
            # Original: Nifty 12-month return > 0
            nifty_lb_idx = nifty_idx - LOOKBACK_DAYS
            if nifty_idx >= 0 and nifty_lb_idx >= 0:
                market_up = nifty.iloc[nifty_idx] > nifty.iloc[nifty_lb_idx]
            else:
                market_up = True

        # ── Candidate selection ────────────────────────────────────────────────
        if not market_up:
            candidates = []
            cash_months += 1
        else:
            top_ranked = returns_12m.nlargest(TOP_N * 2).index.tolist()  # oversample for MA filter

            if use_stock_ma:
                ma_now = ma200.iloc[idx]
                # only keep stocks above their own 200-day MA
                top_ranked = [
                    s for s in top_ranked
                    if s in ma_now.index
                    and not pd.isna(ma_now[s])
                    and not pd.isna(current_px.get(s, np.nan))
                    and current_px[s] > ma_now[s]
                ]

            candidates = top_ranked[:TOP_N]
            if len(candidates) < TOP_N:
                partial_months += 1

        # ── Sell everything ────────────────────────────────────────────────────
        sell_value = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_value += shares * p * (1 - SLIPPAGE_PCT)

        # ── Buy candidates ─────────────────────────────────────────────────────
        held_stocks = {}
        cash_value  = sell_value
        if candidates:
            per_stock = sell_value / TOP_N  # always size as if TOP_N slots
            invested  = 0
            for sym in candidates:
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0:
                    continue
                cost   = per_stock * (1 + SLIPPAGE_PCT)
                shares = cost / p
                held_stocks[sym] = shares
                invested += cost
            cash_value = sell_value - invested
            if cash_value < 0:
                cash_value = 0

        portfolio_value.append(nav)
        dates.append(rebal_date)

    # final NAV
    final_nav = cash_value
    for sym, shares in held_stocks.items():
        if sym in prices.columns:
            p = prices[sym].dropna().iloc[-1]
            final_nav += shares * p
    portfolio_value.append(final_nav)
    dates.append(prices.index[-1])

    nav_s = pd.Series(portfolio_value, index=dates)
    nav_s = nav_s[~nav_s.index.duplicated(keep="last")]

    returns_m = nav_s.pct_change().dropna()
    n_years   = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr      = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / n_years) - 1
    sharpe    = returns_m.mean() / returns_m.std() * np.sqrt(12) if returns_m.std() > 0 else 0
    max_dd    = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()
    total_m   = len(portfolio_value) - 2

    return {
        "label":          label,
        "cagr":           cagr,
        "sharpe":         sharpe,
        "max_dd":         max_dd,
        "final_nav":      nav_s.iloc[-1],
        "cash_months":    cash_months,
        "partial_months": partial_months,
        "total_months":   total_m,
        "nav_series":     nav_s,
    }


configs = [
    (False, False, "A) Baseline (12m return)"),
    (True,  False, "B) Nifty 200MA only"),
    (False, True,  "C) Stock 200MA only"),
    (True,  True,  "D) Both filters"),
]

results = []
for use_nifty_ma, use_stock_ma, label in configs:
    print(f"  Running {label}...", end=" ", flush=True)
    r = run_backtest(use_nifty_ma, use_stock_ma, label)
    results.append(r)
    print(f"CAGR={r['cagr']*100:.1f}%  Sharpe={r['sharpe']:.3f}  MaxDD={r['max_dd']*100:.1f}%  Cash={r['cash_months']}m  Partial={r['partial_months']}m")

# ── Summary ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 80)
print("  DUAL MOMENTUM V3 — DD REDUCTION COMPARISON (TOP_N=50)")
print("=" * 80)
print(f"  {'Config':<28} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'FinalNAV':>16} {'CashMths':>9}")
print(f"  {'-'*28} {'-'*7} {'-'*8} {'-'*8} {'-'*16} {'-'*9}")
for r in results:
    print(f"  {r['label']:<28} {r['cagr']*100:>6.1f}% {r['sharpe']:>8.3f} {r['max_dd']*100:>7.1f}% {r['final_nav']:>16,.0f} {r['cash_months']:>5}/{r['total_months']}")
print("=" * 80)

# ── Year-by-year for all 4 configs side by side ────────────────────────────────
print("\n  Year-by-year returns (all configs):")
print(f"  {'Year':<6} {'Baseline':>10} {'Nifty200MA':>12} {'Stock200MA':>12} {'Both':>10}")
print(f"  {'----':<6} {'--------':>10} {'----------':>12} {'----------':>12} {'----':>10}")

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

# ── Save ───────────────────────────────────────────────────────────────────────
out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
nav_df = pd.DataFrame({r["label"][:1]: r["nav_series"] for r in results})
nav_df.to_csv(out_dir / "dual_momentum_v3_nav.csv")
print(f"\n  NAV saved → {out_dir / 'dual_momentum_v3_nav.csv'}")
