import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

DATA_DIR = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
FNO_SYMS = ["RELIANCE","INFY","TCS","SBIN","HDFCBANK","WIPRO","TATASTEEL","HINDALCO","SAIL","ONGC"]

nifty_raw = yf.download("^NSEI", start="2005-01-01", end="2010-01-01", auto_adjust=True, progress=False)
nifty = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

fno_daily = {}
all_closes = {}
for f in DATA_DIR.glob("*.parquet"):
    sym = f.stem
    df = pd.read_parquet(f)
    df.index = pd.to_datetime(df.index)
    all_closes[sym] = df["close"]
    if sym in FNO_SYMS:
        cols = [c for c in ["open","high","low","close"] if c in df.columns]
        if len(cols) == 4:
            daily = df[cols].resample("D").agg({"open":"first","high":"max","low":"min","close":"last"}).dropna()
            fno_daily[sym] = daily

fno_cols = list(fno_daily.keys())
prices_5min = pd.DataFrame(all_closes).sort_index().loc["2006-01-01":"2010-01-01"]
prices_monthly = prices_5min.resample("ME").last()
fno_close_daily = pd.DataFrame({s: fno_daily[s]["close"] for s in fno_cols}).loc["2006-01-01":"2010-01-01"]
monthly_ends = prices_monthly.index

print(f"fno_cols: {fno_cols}")
print(f"fno_close_daily columns: {list(fno_close_daily.columns)}")
print()

# --- Trace Oct 2008 bear month ---
# Find Oct 2008
oct_2008 = [d for d in monthly_ends if d.year == 2008 and d.month == 10][0]
nov_2008 = [d for d in monthly_ends if d.year == 2008 and d.month == 11][0]
print(f"Oct rebal: {oct_2008}")
print(f"Nov rebal: {nov_2008}")

def get_fno_close(date):
    d = pd.Timestamp(date).normalize()
    idx = fno_close_daily.index.get_indexer([d], method="ffill")[0]
    if idx < 0:
        return pd.Series(dtype=float)
    return fno_close_daily.iloc[idx]

px_oct = get_fno_close(oct_2008)
px_nov = get_fno_close(nov_2008)
print(f"\nOct 2008 FNO prices:\n{px_oct}")
print(f"\nNov 2008 FNO prices:\n{px_nov}")

# Open shorts at Oct 2008
short_budget = 300_000.0
short_stocks = {}
for sym in fno_cols:
    p = px_oct.get(sym, np.nan)
    if pd.isna(p) or p <= 0:
        continue
    per_stock = short_budget / len(fno_cols)
    shares = per_stock * 0.999 / p
    short_stocks[sym] = {"shares": shares, "entry_px": float(p), "alloc": per_stock}

print(f"\nOpened {len(short_stocks)} shorts at Oct 2008")

# Compute MTM at Nov 2008
short_mtm = 0.0
for s, pos in short_stocks.items():
    cp = px_nov.get(s, pos["entry_px"])
    mtm = pos["shares"] * (pos["entry_px"] - cp)
    short_mtm += mtm
    print(f"  {s:15s}: entry={pos['entry_px']:8.2f} exit={cp:8.2f}  pnl={mtm:10.2f}")

print(f"\nTotal short_mtm: {short_mtm:,.2f}")
print(f"Expected: stocks fell in 2008 crash so shorts should PROFIT (positive MTM)")
