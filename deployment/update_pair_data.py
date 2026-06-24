"""
update_pair_data.py
Downloads latest daily closes for all pair symbols from Yahoo Finance.
Run after market close (16:00 IST). Called by scheduler._eod_run().
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import yfinance as yf
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

DATA_DIR = Path(os.getenv("DATA_DIR", r"G:\fyers_data_pipeline\Nifty 500 Daily Data"))

PAIR_SYMS = [
    "TCS", "INFY", "BAJAJFINSV", "BAJFINANCE", "HDFCBANK", "KOTAKBANK",
    "HINDUNILVR", "DABUR", "EICHERMOT", "TVSMOTOR", "OBEROIRLTY", "BRIGADE",
    "TECHM", "COFORGE", "TATAPOWER", "JSWENERGY", "HDFCLIFE", "ICICIPRULI",
    "SRF", "DEEPAKNTR",
]


def _flatten_cols(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    return df


def update_symbols(symbols: list[str] = PAIR_SYMS) -> None:
    today = datetime.today().strftime("%Y-%m-%d")

    for sym in symbols:
        out = DATA_DIR / f"{sym}.parquet"
        if out.exists():
            df_old = pd.read_parquet(out)
            last = df_old.index[-1]
            start = (last + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            df_old = None
            start = "2005-01-01"

        if start >= today:
            print(f"  [data] {sym}: up to date ({last.date()})")
            continue

        print(f"  [data] {sym}: fetching {start} -> {today} ...", end=" ", flush=True)
        try:
            df_new = yf.download(f"{sym}.NS", start=start, end=today,
                                 auto_adjust=True, progress=False)
            if df_new.empty:
                print("no new data")
                continue

            df_new = _flatten_cols(df_new)
            df_new.index = pd.to_datetime(df_new.index)
            df_new = df_new.dropna(subset=["close"])  # drop partial/unsettled rows

            if df_new.empty:
                print("no settled closes")
                continue

            if df_old is not None:
                df_combined = pd.concat([df_old, df_new[~df_new.index.isin(df_old.index)]])
            else:
                df_combined = df_new

            df_combined.to_parquet(out)
            print(f"OK -> last={df_combined.index[-1].date()}, close={df_combined['close'].iloc[-1]:.2f}")
        except Exception as e:
            print(f"ERROR: {e}")


if __name__ == "__main__":
    update_symbols()
