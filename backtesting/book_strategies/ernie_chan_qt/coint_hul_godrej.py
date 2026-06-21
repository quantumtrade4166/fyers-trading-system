"""
coint_hul_godrej.py
Cointegration analysis for HINDUNILVR / GODREJCP pair.
Reports: ADF stat, half-life, OLS beta, lot balance recommendation.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
from statsmodels.tsa.stattools import adfuller
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

CACHE = Path("backtesting/book_strategies/ernie_chan_qt/data/hul_godrej_daily_2015_2024.parquet")
data  = pd.read_parquet(CACHE)
data  = data.dropna()

pa = data["HINDUNILVR"].values
pb = data["GODREJCP"].values
dates = data.index

print(f"Data: {dates[0].date()} to {dates[-1].date()}  ({len(data)} rows)")
print(f"HINDUNILVR price range: {pa.min():.0f} – {pa.max():.0f}")
print(f"GODREJCP   price range: {pb.min():.0f} – {pb.max():.0f}")

# ── OLS regression: HINDUNILVR = alpha + beta * GODREJCP ─────────────────────
X = add_constant(pb)
res = OLS(pa, X).fit()
alpha, beta = res.params
spread = pa - beta * pb

print(f"\n── OLS Regression: HINDUNILVR = {alpha:.2f} + {beta:.4f} × GODREJCP")
print(f"   R²     = {res.rsquared:.4f}")
print(f"   Beta   = {beta:.4f}")

# ── ADF test on spread ────────────────────────────────────────────────────────
adf_result = adfuller(spread, autolag="AIC")
adf_stat   = adf_result[0]
p_val      = adf_result[1]
crit       = adf_result[4]

print(f"\n── ADF Test on Spread (HINDUNILVR − {beta:.4f}×GODREJCP)")
print(f"   ADF Statistic : {adf_stat:.4f}")
print(f"   p-value       : {p_val:.4f}")
print(f"   Critical values:")
for level, val in crit.items():
    sig = " ← PASSES" if adf_stat < val else ""
    print(f"     {level}:  {val:.4f}{sig}")

if adf_stat < crit["5%"]:
    print(f"\n   RESULT: COINTEGRATED at 5% significance ✓")
elif adf_stat < crit["10%"]:
    print(f"\n   RESULT: COINTEGRATED at 10% significance (borderline)")
else:
    print(f"\n   RESULT: NOT COINTEGRATED — do not trade this pair")

# ── Half-life (AR(1) on spread) ───────────────────────────────────────────────
spread_lag  = spread[:-1]
spread_diff = np.diff(spread)
X2 = add_constant(spread_lag)
ar_res = OLS(spread_diff, X2).fit()
phi = ar_res.params[1]
half_life = -np.log(2) / np.log(1 + phi) if phi < 0 else float("inf")

print(f"\n── Half-Life of Mean Reversion")
print(f"   AR(1) phi   = {phi:.6f}")
print(f"   Half-life   = {half_life:.1f} days")
if half_life < 5:
    print(f"   WARNING: very fast reversion — may be noise")
elif half_life > 200:
    print(f"   WARNING: very slow reversion — needs very long lookback")
else:
    print(f"   Recommended LOOKBACK = {int(half_life * 2)} days (2× half-life)")

# ── Lot size recommendation ───────────────────────────────────────────────────
# NSE F&O lot sizes (as of 2024-25)
# HINDUNILVR: 300 shares/lot (verify — can change each expiry review)
# GODREJCP:   500 shares/lot
LOT_HUL   = 300
LOT_GCP   = 500

hul_avg = pa[-252:].mean()   # recent 1-year avg price
gcp_avg = pb[-252:].mean()

margin_hul = hul_avg * LOT_HUL * 0.15   # ~15% SPAN margin
margin_gcp = gcp_avg * LOT_GCP * 0.15

# Correct lot balance: need beta × LOT_HUL shares of GODREJCP
shares_gcp_needed = beta * LOT_HUL
lots_gcp_needed   = shares_gcp_needed / LOT_GCP

print(f"\n── Lot Balance Calculation")
print(f"   OLS Beta                  = {beta:.4f}")
print(f"   HUL lot size              = {LOT_HUL} shares")
print(f"   GCP lot size              = {LOT_GCP} shares")
print(f"   GCP shares needed         = {beta:.4f} × {LOT_HUL} = {shares_gcp_needed:.1f}")
print(f"   GCP lots needed           = {shares_gcp_needed:.1f} / {LOT_GCP} = {lots_gcp_needed:.3f}")
print(f"   Round to nearest lot      = {round(lots_gcp_needed)} lot(s) = {round(lots_gcp_needed) * LOT_GCP} shares")
imbalance = abs(round(lots_gcp_needed) * LOT_GCP - shares_gcp_needed) / shares_gcp_needed * 100
print(f"   Lot imbalance             = {imbalance:.1f}%")

print(f"\n── Margin Estimate (15% SPAN, recent prices)")
print(f"   HUL avg (1yr)   = Rs{hul_avg:.0f}  × {LOT_HUL} shares = Rs{hul_avg*LOT_HUL:,.0f} notional → margin ~Rs{margin_hul:,.0f}")
print(f"   GCP avg (1yr)   = Rs{gcp_avg:.0f}  × {LOT_GCP} shares = Rs{gcp_avg*LOT_GCP:,.0f} notional → margin ~Rs{margin_gcp:,.0f}")
print(f"   1 HUL + {round(lots_gcp_needed)} GCP lots → total margin ~Rs{margin_hul + round(lots_gcp_needed)*margin_gcp:,.0f}")

# ── Spread stats ──────────────────────────────────────────────────────────────
spread_std  = spread.std()
spread_mean = spread.mean()
print(f"\n── Spread Statistics")
print(f"   Mean   = {spread_mean:.2f}")
print(f"   Std    = {spread_std:.2f}")
print(f"   At ENTRY_Z=2.0, spread must move {2*spread_std:.2f} from mean")
print(f"   At STOP_Z=3.5,  spread moves    {3.5*spread_std:.2f} from mean")

# ── Rolling half-life stability check ────────────────────────────────────────
print(f"\n── Rolling Half-Life (63-day windows) — stability check")
hls = []
for t in range(252, len(spread), 63):
    s = spread[t-252:t]
    sd = np.diff(s)
    sl = s[:-1]
    X3 = add_constant(sl)
    try:
        phi3 = OLS(sd, X3).fit().params[1]
        hl3 = -np.log(2) / np.log(1 + phi3) if phi3 < 0 else 999
        hls.append((dates[t], round(hl3, 1)))
    except Exception:
        pass

for dt, hl in hls[::2]:   # print every other to keep concise
    print(f"   {dt.date()}: HL = {hl:.0f}d")
