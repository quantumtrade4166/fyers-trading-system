"""
download_cipla_drreddy.py
Download CIPLA + DRREDDY daily closes from Yahoo Finance (2015-2024)
and stitch with Fyers data (2024-2026). Saves to data/cipla_drreddy_daily.parquet
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
PROJECT_ROOT = Path(".").resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv

OUT = Path("backtesting/book_strategies/ernie_chan_qt/data")
OUT.mkdir(parents=True, exist_ok=True)
OUTFILE = OUT / "cipla_drreddy_daily.parquet"

print("Downloading Yahoo Finance data 2015-2024...")
import yfinance as yf
tickers = {"CIPLA": "CIPLA.NS", "DRREDDY": "DRREDDY.NS"}
yf_data = {}
for name, ticker in tickers.items():
    df = yf.download(ticker, start="2015-01-01", end="2024-05-28",
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).normalize()
    yf_data[name] = df["Close"].rename(name)
    print(f"  {name}: {len(df)} rows, {df.index[0].date()} to {df.index[-1].date()}")
yf_df = pd.DataFrame(yf_data).dropna()

print("\nLoading Fyers data 2024-2026...")
loader = DataLoader()
raw = loader.load_many(["NSE:CIPLA-EQ", "NSE:DRREDDY-EQ"])
fy_data = {}
for sym, df in raw.items():
    d = resample_ohlcv(df, "1D")
    d.index = d.index.normalize()
    name = sym.split(":")[1].replace("-EQ", "")
    fy_data[name] = d["close"]
    print(f"  {name}: {len(d)} rows, {d.index[0].date()} to {d.index[-1].date()}")
fy_df = pd.DataFrame(fy_data).dropna()

cutoff = pd.Timestamp("2024-05-27")
combined = pd.concat([yf_df[yf_df.index <= cutoff],
                      fy_df[fy_df.index > cutoff]]).sort_index()
combined = combined[~combined.index.duplicated(keep="last")].dropna()

print(f"\nFinal panel: {len(combined)} rows, {combined.index[0].date()} to {combined.index[-1].date()}")
print(combined.tail())
combined.to_parquet(OUTFILE)
print(f"Saved to {OUTFILE}")
