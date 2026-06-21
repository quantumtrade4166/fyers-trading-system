import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import time
import requests
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime

DATA_DIR = Path("G:/fyers_data_pipeline/Nifty 500 Daily Data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = "2005-01-01"
END_DATE   = datetime.today().strftime("%Y-%m-%d")

NSE_CSV_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"


def get_nifty500_symbols():
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(NSE_CSV_URL, headers=headers, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(pd.io.common.BytesIO(r.content))
    symbols = df["Symbol"].str.strip().tolist()
    return symbols


def nse_to_yf(symbol: str) -> str:
    return symbol + ".NS"


def download_symbol(symbol: str) -> bool:
    yf_sym = nse_to_yf(symbol)
    out_path = DATA_DIR / f"{symbol}.parquet"

    try:
        df = yf.download(yf_sym, start=START_DATE, end=END_DATE,
                         auto_adjust=True, progress=False)
        if df.empty or len(df) < 100:
            return False

        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.index.name = "date"
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]]
        df["symbol"] = f"NSE:{symbol}-EQ"
        df.to_parquet(out_path)
        return True
    except Exception as e:
        print(f"  ERROR {symbol}: {e}")
        return False


def main():
    print(f"Fetching Nifty 500 list...")
    symbols = get_nifty500_symbols()
    print(f"Total symbols: {len(symbols)}")
    print(f"Saving to: {DATA_DIR}")
    print(f"Date range: {START_DATE} to {END_DATE}")
    print("-" * 60)

    done, failed, skipped = [], [], []

    for i, sym in enumerate(symbols, 1):
        out_path = DATA_DIR / f"{sym}.parquet"
        if out_path.exists():
            skipped.append(sym)
            print(f"[{i:3d}/{len(symbols)}] SKIP  {sym}")
            continue

        success = download_symbol(sym)
        if success:
            done.append(sym)
            size = (DATA_DIR / f"{sym}.parquet").stat().st_size // 1024
            print(f"[{i:3d}/{len(symbols)}] OK    {sym}  ({size} KB)")
        else:
            failed.append(sym)
            print(f"[{i:3d}/{len(symbols)}] FAIL  {sym}")

        # Polite delay to avoid rate limiting
        time.sleep(0.3)

    print("\n" + "=" * 60)
    print(f"Downloaded : {len(done)}")
    print(f"Skipped    : {len(skipped)}")
    print(f"Failed     : {len(failed)}")
    if failed:
        print(f"Failed list: {failed}")


if __name__ == "__main__":
    main()
