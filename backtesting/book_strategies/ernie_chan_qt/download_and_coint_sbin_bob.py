"""
download_and_coint_sbin_bob.py
Download + cointegration check for SBIN / BANKBARODA (PSU Banking pair).
Two largest PSU banks — both driven by govt policy, NPA cycles, credit growth.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
PROJECT_ROOT = Path(".").resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np
import yfinance as yf
from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv
from statsmodels.tsa.stattools import adfuller
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

OUT = Path("backtesting/book_strategies/ernie_chan_qt/data")
OUT.mkdir(parents=True, exist_ok=True)
OUTFILE = OUT / "sbin_bob_daily.parquet"

# ── Download ──────────────────────────────────────────────────────────────────
print("Downloading Yahoo Finance 2015-2024...")
yf_data = {}
for name, ticker in [("SBIN", "SBIN.NS"), ("BANKBARODA", "BANKBARODA.NS")]:
    df = yf.download(ticker, start="2015-01-01", end="2024-05-28",
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).normalize()
    yf_data[name] = df["Close"].rename(name)
    print(f"  {name}: {len(df)} rows, {df.index[0].date()} to {df.index[-1].date()}")
yf_df = pd.DataFrame(yf_data).dropna()

print("Loading Fyers 2024-2026...")
loader = DataLoader()
raw = loader.load_many(["NSE:SBIN-EQ", "NSE:BANKBARODA-EQ"])
fy_data = {}
for sym, df in raw.items():
    d = resample_ohlcv(df, "1D")
    d.index = d.index.normalize()
    name = sym.split(":")[1].replace("-EQ", "")
    fy_data[name] = d["close"]
    print(f"  {name}: {len(d)} rows")
fy_df = pd.DataFrame(fy_data).dropna()

cutoff = pd.Timestamp("2024-05-27")
data   = pd.concat([yf_df[yf_df.index <= cutoff],
                    fy_df[fy_df.index > cutoff]]).sort_index()
data   = data[~data.index.duplicated(keep="last")].dropna()
data.to_parquet(OUTFILE)
print(f"Saved: {len(data)} rows, {data.index[0].date()} to {data.index[-1].date()}")

# ── Cointegration analysis ────────────────────────────────────────────────────
pa, pb, dates = data["SBIN"].values, data["BANKBARODA"].values, data.index

print(f"\nSBIN range:       Rs{pa.min():.0f} – Rs{pa.max():.0f}")
print(f"BANKBARODA range: Rs{pb.min():.0f} – Rs{pb.max():.0f}")

res   = OLS(pa, add_constant(pb)).fit()
alpha, beta = res.params
spread = pa - beta * pb

print(f"\n── OLS: SBIN = {alpha:.2f} + {beta:.4f} × BANKBARODA   R²={res.rsquared:.4f}")

adf  = adfuller(spread, autolag="AIC")
crit = adf[4]
print(f"\n── ADF on Spread")
print(f"   Statistic : {adf[0]:.4f}   p-value: {adf[1]:.4f}")
for lvl, val in crit.items():
    sig = " ← PASSES" if adf[0] < val else ""
    print(f"   {lvl}: {val:.4f}{sig}")
if   adf[0] < crit["1%"]:  print("\n   RESULT: COINTEGRATED at 1%  ✓✓")
elif adf[0] < crit["5%"]:  print("\n   RESULT: COINTEGRATED at 5%  ✓")
elif adf[0] < crit["10%"]: print("\n   RESULT: COINTEGRATED at 10% (borderline)")
else:                       print("\n   RESULT: NOT COINTEGRATED")

phi = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
hl  = -np.log(2) / np.log(1 + phi) if phi < 0 else float("inf")
print(f"\n── Half-Life: {hl:.1f} days  → LOOKBACK = {int(hl*2)} days")

# Lot sizes
LOT_SBIN = 1500
LOT_BOB  = 3500
sbin_avg = pa[-252:].mean()
bob_avg  = pb[-252:].mean()
shares_bob = beta * LOT_SBIN
lots_bob   = shares_bob / LOT_BOB
margin_s = sbin_avg * LOT_SBIN * 0.15
margin_b = bob_avg  * LOT_BOB  * 0.15
print(f"\n── Lot Balance")
print(f"   SBIN lot  = {LOT_SBIN} shares  (avg Rs{sbin_avg:.0f}, margin ~Rs{margin_s:,.0f})")
print(f"   BOB  lot  = {LOT_BOB} shares  (avg Rs{bob_avg:.0f},  margin ~Rs{margin_b:,.0f})")
print(f"   Beta × SBIN = {beta:.4f} × {LOT_SBIN} = {shares_bob:.1f} BOB shares needed")
print(f"   = {lots_bob:.2f} BOB lots → round to {round(lots_bob)} lot(s) = {round(lots_bob)*LOT_BOB} shares")
print(f"   Imbalance: {abs(round(lots_bob)*LOT_BOB - shares_bob)/shares_bob*100:.1f}%")
print(f"   Capital: ~Rs{margin_s + round(lots_bob)*margin_b:,.0f}")

print(f"\n── Rolling Half-Life (252d windows, every 126d)")
for t in range(252, len(spread), 126):
    s = spread[t-252:t]
    try:
        phi3 = OLS(np.diff(s), add_constant(s[:-1])).fit().params[1]
        hl3  = -np.log(2) / np.log(1 + phi3) if phi3 < 0 else 999
        print(f"   {dates[t].date()}: HL = {hl3:.0f}d")
    except Exception:
        pass
