import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
import yfinance as yf
from pathlib import Path
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

RESULTS_DIR = Path(r"G:\fyers_data_pipeline\backtesting\book_strategies\ernie_chan_qt\results")

# ── Load portfolio daily equity ───────────────────────────────────────────────
eq = pd.read_csv(RESULTS_DIR / "portfolio_daily_equity.csv", parse_dates=["date"])
eq = eq.set_index("date").sort_index()

# Daily P&L from equity series
eq["daily_pnl"] = eq["equity"].diff()

# Two return bases:
#   1. On span_cap (deployed capital that day) — most meaningful for a paired strategy
#   2. On equity NAV — what a fund would report
eq["ret_on_span"] = eq["daily_pnl"] / eq["span_cap"].shift(1)
eq["ret_on_nav"]  = eq["daily_pnl"] / eq["equity"].shift(1)
eq = eq.dropna()

print(f"Portfolio data: {eq.index[0].date()} → {eq.index[-1].date()}  ({len(eq)} days)")

# ── Download Nifty 50 ─────────────────────────────────────────────────────────
print("Downloading Nifty 50 (^NSEI)...")
nifty_raw = yf.download("^NSEI",
                         start=str(eq.index[0].date()),
                         end=str(eq.index[-1].date()),
                         auto_adjust=True, progress=False)
if isinstance(nifty_raw.columns, pd.MultiIndex):
    nifty_raw.columns = nifty_raw.columns.get_level_values(0)
nifty_raw.index = pd.to_datetime(nifty_raw.index).normalize()
nifty_ret = nifty_raw["Close"].pct_change().dropna().rename("nifty_ret")

# ── Merge on common trading days ─────────────────────────────────────────────
df = eq[["ret_on_span", "ret_on_nav"]].join(nifty_ret, how="inner").dropna()
print(f"Common trading days: {len(df)}  ({df.index[0].date()} → {df.index[-1].date()})")

# ── Risk-free rate (India 10-yr G-Sec average 2015–2026 ≈ 6.5% p.a.) ────────
RF_ANNUAL = 0.065
rf_daily  = RF_ANNUAL / 252

df["rf"]            = rf_daily
df["nifty_excess"]  = df["nifty_ret"]    - df["rf"]
df["port_excess_span"] = df["ret_on_span"] - df["rf"]
df["port_excess_nav"]  = df["ret_on_nav"]  - df["rf"]

years = len(df) / 252

# ── CAPM regression ───────────────────────────────────────────────────────────
def capm(port_excess, label):
    X = add_constant(df["nifty_excess"])
    res = OLS(port_excess, X).fit()
    alpha_daily = res.params["const"]
    beta        = res.params["nifty_excess"]
    r2          = res.rsquared

    alpha_annual = alpha_daily * 252
    alpha_pct    = alpha_annual * 100

    # Annualised return and vol
    ann_ret  = port_excess.mean() * 252 + RF_ANNUAL
    ann_vol  = port_excess.std()  * np.sqrt(252)
    sharpe   = (ann_ret - RF_ANNUAL) / ann_vol if ann_vol > 0 else 0
    treynor  = (ann_ret - RF_ANNUAL) / beta    if abs(beta) > 1e-6 else 0
    info_ratio = alpha_annual / (res.resid.std() * np.sqrt(252)) if res.resid.std() > 0 else 0

    print(f"\n{'─'*55}")
    print(f"  Basis: {label}")
    print(f"{'─'*55}")
    print(f"  Jensen's Alpha  : {alpha_pct:+.2f}% per year")
    print(f"  Beta vs Nifty   : {beta:.4f}")
    print(f"  R² (CAPM fit)   : {r2:.4f}")
    print(f"  Ann. Return     : {ann_ret*100:.2f}%")
    print(f"  Ann. Volatility : {ann_vol*100:.2f}%")
    print(f"  Sharpe Ratio    : {sharpe:.3f}")
    print(f"  Treynor Ratio   : {treynor:.4f}")
    print(f"  Info Ratio      : {info_ratio:.3f}")
    print(f"  t-stat (alpha)  : {res.tvalues['const']:.3f}  (p={res.pvalues['const']:.4f})")
    print(f"  t-stat (beta)   : {res.tvalues['nifty_excess']:.3f}  (p={res.pvalues['nifty_excess']:.4f})")

    return alpha_annual, beta

# ── Nifty stats for reference ─────────────────────────────────────────────────
nifty_ann_ret = df["nifty_ret"].mean() * 252
nifty_ann_vol = df["nifty_ret"].std()  * np.sqrt(252)
nifty_sharpe  = (nifty_ann_ret - RF_ANNUAL) / nifty_ann_vol

print(f"\n{'='*55}")
print(f"  JENSEN'S ALPHA — 10-Pair Pairs Trading Portfolio")
print(f"{'='*55}")
print(f"\n  Risk-free rate    : {RF_ANNUAL*100:.1f}% p.a. (India 10-yr G-Sec avg)")
print(f"  Market (Nifty 50) : {nifty_ann_ret*100:.2f}% p.a.  Sharpe {nifty_sharpe:.3f}")

capm(df["port_excess_span"], "Return on deployed SPAN capital")
capm(df["port_excess_nav"],  "Return on equity NAV")

print(f"\n{'='*55}")
print(f"  INTERPRETATION")
print(f"{'='*55}")
print(f"  Jensen's Alpha = return earned ABOVE what CAPM predicts")
print(f"  given the strategy's beta to the market.")
print(f"  For a market-neutral pairs strategy, beta ≈ 0,")
print(f"  so Alpha ≈ Ann.Return − Risk-Free Rate.")
print(f"  A statistically significant alpha (p < 0.05) means")
print(f"  the strategy generates genuine skill-based return.")
