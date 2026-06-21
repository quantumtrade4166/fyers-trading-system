"""
coint_cipla_drreddy.py
Cointegration analysis for CIPLA / DRREDDY pair.
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

CACHE = Path("backtesting/book_strategies/ernie_chan_qt/data/cipla_drreddy_daily.parquet")
data  = pd.read_parquet(CACHE).dropna()
pa    = data["CIPLA"].values      # sym A
pb    = data["DRREDDY"].values    # sym B
dates = data.index

print(f"Data: {dates[0].date()} to {dates[-1].date()}  ({len(data)} rows)")
print(f"CIPLA   price range: {pa.min():.0f} – {pa.max():.0f}")
print(f"DRREDDY price range: {pb.min():.0f} – {pb.max():.0f}")

# ── OLS: CIPLA = alpha + beta * DRREDDY ──────────────────────────────────────
res   = OLS(pa, add_constant(pb)).fit()
alpha, beta = res.params
spread = pa - beta * pb

print(f"\n── OLS: CIPLA = {alpha:.2f} + {beta:.4f} × DRREDDY   R²={res.rsquared:.4f}")

# ── ADF on spread ─────────────────────────────────────────────────────────────
adf = adfuller(spread, autolag="AIC")
crit = adf[4]
print(f"\n── ADF Test on Spread")
print(f"   Statistic : {adf[0]:.4f}   p-value: {adf[1]:.4f}")
for lvl, val in crit.items():
    sig = " ← PASSES" if adf[0] < val else ""
    print(f"   {lvl}: {val:.4f}{sig}")
if   adf[0] < crit["1%"]:  print("\n   RESULT: COINTEGRATED at 1%  ✓✓")
elif adf[0] < crit["5%"]:  print("\n   RESULT: COINTEGRATED at 5%  ✓")
elif adf[0] < crit["10%"]: print("\n   RESULT: COINTEGRATED at 10% (borderline)")
else:                       print("\n   RESULT: NOT COINTEGRATED — skip this pair")

# ── Half-life ─────────────────────────────────────────────────────────────────
phi = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
hl  = -np.log(2) / np.log(1 + phi) if phi < 0 else float("inf")
print(f"\n── Half-Life: {hl:.1f} days  (phi={phi:.6f})")
print(f"   Recommended LOOKBACK = {int(hl*2)} days")

# ── Lot balance ───────────────────────────────────────────────────────────────
LOT_CIPLA   = 650   # NSE F&O lot sizes
LOT_DRREDDY = 125
cipla_avg   = pa[-252:].mean()
drd_avg     = pb[-252:].mean()
shares_drd  = beta * LOT_CIPLA
lots_drd    = shares_drd / LOT_DRREDDY
margin_c    = cipla_avg * LOT_CIPLA * 0.15
margin_d    = drd_avg * LOT_DRREDDY * 0.15

print(f"\n── Lot Balance")
print(f"   CIPLA lot  = {LOT_CIPLA} shares  (avg price Rs{cipla_avg:.0f}, margin ~Rs{margin_c:,.0f})")
print(f"   DRR lot    = {LOT_DRREDDY} shares  (avg price Rs{drd_avg:.0f}, margin ~Rs{margin_d:,.0f})")
print(f"   Beta × CIPLA lot = {beta:.4f} × {LOT_CIPLA} = {shares_drd:.1f} DRREDDY shares needed")
print(f"   = {lots_drd:.2f} DRR lots  →  round to {round(lots_drd)} lot(s) = {round(lots_drd)*LOT_DRREDDY} shares")
imb = abs(round(lots_drd)*LOT_DRREDDY - shares_drd) / shares_drd * 100
print(f"   Lot imbalance: {imb:.1f}%")
print(f"   Total margin (1 CIPLA + {round(lots_drd)} DRR): ~Rs{margin_c + round(lots_drd)*margin_d:,.0f}")

# ── Spread stats ──────────────────────────────────────────────────────────────
print(f"\n── Spread Stats   mean={spread.mean():.1f}  std={spread.std():.1f}")

# ── Rolling half-life ─────────────────────────────────────────────────────────
print(f"\n── Rolling Half-Life (252-day windows, every 63 days)")
for t in range(252, len(spread), 63):
    s = spread[t-252:t]
    try:
        phi3 = OLS(np.diff(s), add_constant(s[:-1])).fit().params[1]
        hl3  = -np.log(2) / np.log(1 + phi3) if phi3 < 0 else 999
        print(f"   {dates[t].date()}: HL = {hl3:.0f}d")
    except Exception:
        pass
