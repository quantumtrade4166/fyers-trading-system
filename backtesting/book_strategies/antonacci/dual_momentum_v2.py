"""
Dual Momentum V2 — Gary Antonacci
- Absolute filter: Nifty 50 12-month return > 0% (not a static hurdle)
- Compares TOP_N = 5, 10, 20, 50 in one run
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
CAPITAL       = 1_000_000
SLIPPAGE_PCT  = 0.001
START_DATE    = "2006-01-01"
END_DATE      = "2026-06-18"
TOP_N_LIST    = [5, 10, 20, 50]

# ── Load Nifty 50 index ────────────────────────────────────────────────────────
print("Downloading Nifty 50 index (^NSEI)...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty.name = "NIFTY"
print(f"Nifty: {nifty.index[0].date()} to {nifty.index[-1].date()}, {len(nifty)} days")

# ── Load 500 symbols ───────────────────────────────────────────────────────────
print("Loading 500 symbols...")
frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]

prices = pd.DataFrame(frames).sort_index()
prices = prices.loc[START_DATE:END_DATE]
print(f"Price matrix: {prices.shape[0]} days x {prices.shape[1]} symbols\n")

monthly_ends = prices.resample("ME").last().index


def run_backtest(top_n):
    portfolio_value = [CAPITAL]
    dates           = [monthly_ends[0]]
    holdings_log    = []
    cash_value      = CAPITAL
    held_stocks     = {}

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
        # snap to actual trading day
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date    = prices.index[idx]
        current_px    = prices.iloc[idx]

        # mark-to-market
        nav = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                nav += shares * p

        # need enough history
        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            portfolio_value.append(nav)
            dates.append(rebal_date)
            continue

        past_px    = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px) - 1
        returns_12m = returns_12m.dropna()

        # ── Absolute filter: is Nifty up over last 12 months? ─────────────────
        nifty_idx_now  = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        nifty_idx_past = nifty_idx_now - LOOKBACK_DAYS
        if nifty_idx_past >= 0:
            nifty_return_12m = (nifty.iloc[nifty_idx_now] / nifty.iloc[nifty_idx_past]) - 1
            market_up = nifty_return_12m > 0
        else:
            market_up = True  # not enough history, stay invested

        # ── Relative momentum: top N ──────────────────────────────────────────
        if market_up:
            candidates = returns_12m.nlargest(top_n).index.tolist()
        else:
            candidates = []  # go to cash

        # ── Sell everything ───────────────────────────────────────────────────
        sell_value = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_value += shares * p * (1 - SLIPPAGE_PCT)

        # ── Buy candidates ────────────────────────────────────────────────────
        held_stocks = {}
        cash_value  = sell_value
        if candidates:
            per_stock = sell_value / top_n
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

        holdings_log.append({"date": rebal_date, "in_market": len(candidates), "market_up": market_up})
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

    returns_m   = nav_s.pct_change().dropna()
    n_years     = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr        = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / n_years) - 1
    sharpe      = returns_m.mean() / returns_m.std() * np.sqrt(12) if returns_m.std() > 0 else 0
    max_dd      = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()
    cash_months = sum(1 for h in holdings_log if not h["market_up"])
    total_m     = len(holdings_log)

    return {
        "top_n":       top_n,
        "cagr":        cagr,
        "sharpe":      sharpe,
        "max_dd":      max_dd,
        "final_nav":   nav_s.iloc[-1],
        "cash_months": cash_months,
        "total_months": total_m,
        "nav_series":  nav_s,
        "holdings_log": holdings_log,
    }


# ── Run all configurations ─────────────────────────────────────────────────────
results = []
for n in TOP_N_LIST:
    print(f"  Running TOP_N={n}...", end=" ", flush=True)
    r = run_backtest(n)
    results.append(r)
    print(f"CAGR={r['cagr']*100:.1f}%  Sharpe={r['sharpe']:.3f}  MaxDD={r['max_dd']*100:.1f}%  CashMonths={r['cash_months']}/{r['total_months']}")

# ── Summary table ──────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("  DUAL MOMENTUM V2 — NIFTY ABSOLUTE FILTER — COMPARISON")
print("=" * 70)
print(f"  {'TOP_N':<8} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>8} {'FinalNAV':>16} {'CashMths':>10}")
print(f"  {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*16} {'-'*10}")
for r in results:
    print(f"  {r['top_n']:<8} {r['cagr']*100:>7.1f}% {r['sharpe']:>8.3f} {r['max_dd']*100:>7.1f}% {r['final_nav']:>16,.0f} {r['cash_months']:>4}/{r['total_months']}")
print("=" * 70)

# ── Year-by-year for best Sharpe ───────────────────────────────────────────────
best = max(results, key=lambda x: x["sharpe"])
print(f"\n  Year-by-year for best Sharpe (TOP_N={best['top_n']}):")
print(f"  {'Year':<6} {'Return':>8} {'NAV':>14}")
print(f"  {'----':<6} {'------':>8} {'---':>14}")
annual = best["nav_series"].resample("YE").last()
for i in range(1, len(annual)):
    yr  = annual.index[i].year
    ret = (annual.iloc[i] / annual.iloc[i - 1]) - 1
    flag = " ← cash" if not best["holdings_log"][i-1]["market_up"] else ""
    print(f"  {yr:<6} {ret*100:>7.1f}% {annual.iloc[i]:>14,.0f}{flag}")

# ── Save NAVs ─────────────────────────────────────────────────────────────────
out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
nav_df = pd.DataFrame({f"top_{r['top_n']}": r["nav_series"] for r in results})
nav_df.to_csv(out_dir / "dual_momentum_v2_nav.csv")
print(f"\n  NAV saved → {out_dir / 'dual_momentum_v2_nav.csv'}")
