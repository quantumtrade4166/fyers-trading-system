"""
download_hul_godrej.py
Download HINDUNILVR + GODREJCP daily closes from Yahoo Finance (2015-2024)
and stitch with Fyers 5-min data (2024-2026) resampled to daily.
Saves to data/hul_godrej_daily_2015_2024.parquet
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
PROJECT_ROOT = Path(".").resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np
from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv

OUT = Path("backtesting/book_strategies/ernie_chan_qt/data")
OUT.mkdir(parents=True, exist_ok=True)
OUTFILE = OUT / "hul_godrej_daily_2015_2024.parquet"

# ── Yahoo Finance download (2015-2024) ────────────────────────────────────────
print("Downloading Yahoo Finance data 2015-2024...")
try:
    import yfinance as yf
    tickers = {"HINDUNILVR": "HINDUNILVR.NS", "GODREJCP": "GODREJCP.NS"}
    yf_data = {}
    for name, ticker in tickers.items():
        df = yf.download(ticker, start="2015-01-01", end="2024-05-28",
                         auto_adjust=True, progress=False)
        if df.empty:
            print(f"  WARNING: no data for {ticker}")
            continue
        # Handle MultiIndex columns from newer yfinance
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index).normalize()
        yf_data[name] = df["Close"].rename(name)
        print(f"  {name}: {len(df)} rows, {df.index[0].date()} to {df.index[-1].date()}")
    yf_df = pd.DataFrame(yf_data).dropna()
    print(f"  Combined: {len(yf_df)} rows after dropna")
except Exception as e:
    print(f"  yfinance error: {e}")
    yf_df = pd.DataFrame()

# ── Fyers recent data (2024-2026) ─────────────────────────────────────────────
print("\nLoading Fyers data 2024-2026...")
loader = DataLoader()
raw = loader.load_many(["NSE:HINDUNILVR-EQ", "NSE:GODREJCP-EQ"])
fy_data = {}
for sym, df in raw.items():
    d = resample_ohlcv(df, "1D")
    d.index = d.index.normalize()
    name = sym.split(":")[1].replace("-EQ", "")
    fy_data[name] = d["close"]
    print(f"  {name}: {len(d)} rows, {d.index[0].date()} to {d.index[-1].date()}")
fy_df = pd.DataFrame(fy_data).dropna()

# ── Stitch ────────────────────────────────────────────────────────────────────
cutoff = pd.Timestamp("2024-05-27")
if not yf_df.empty:
    combined = pd.concat([yf_df[yf_df.index <= cutoff],
                          fy_df[fy_df.index > cutoff]]).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")].dropna()
else:
    print("  WARNING: Yahoo data missing — using Fyers only")
    combined = fy_df

print(f"\nFinal panel: {len(combined)} rows, {combined.index[0].date()} to {combined.index[-1].date()}")
print(combined.tail())

combined.to_parquet(OUTFILE)
print(f"\nSaved to {OUTFILE}")
