"""
fetch_nifty_index_fyers.py
Save NIFTY 50 daily closes from Fyers, replacing the yfinance "^NSEI" call.

yfinance was the single point of failure in the live signal: on 2026-08-31 at
16:00 it returned ZERO rows for ^NSEI and the month-end rebalance aborted.
Fyers has the index back to 2005.

Output: Nifty 500 Daily Fyers/_NIFTY50_INDEX.parquet
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import json
import time
from pathlib import Path

import pandas as pd
from fyers_apiv3 import fyersModel

ROOT   = Path(r"G:\fyers_data_pipeline")
OUT    = ROOT / "Nifty 500 Daily Fyers" / "_NIFTY50_INDEX.parquet"
SYMBOL = "NSE:NIFTY50-INDEX"

tok = json.loads((ROOT / "config" / "access_token.txt").read_text(encoding="utf-8"))["token"]
fy = fyersModel.FyersModel(client_id=f"W09OMXQB8J-100:{tok}", is_async=False,
                           token=tok, log_path="")

frames = []
for year in range(2005, 2027):
    r = fy.history({"symbol": SYMBOL, "resolution": "D", "date_format": "1",
                    "range_from": f"{year}-01-01", "range_to": f"{year}-12-31",
                    "cont_flag": "1"})
    if r.get("s") != "ok" or not r.get("candles"):
        print(f"  {year}: no data ({r.get('s')})")
        continue
    d = pd.DataFrame(r["candles"], columns=["ts", "open", "high", "low", "close", "volume"])
    d["date"] = (pd.to_datetime(d["ts"], unit="s", utc=True)
                   .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).dt.normalize())
    frames.append(d.drop(columns=["ts"]).set_index("date"))
    print(f"  {year}: {len(d)} bars")
    time.sleep(0.15)

df = pd.concat(frames).sort_index()
df = df[~df.index.duplicated(keep="last")]
OUT.parent.mkdir(exist_ok=True)
df.to_parquet(OUT)

print(f"\nSaved {len(df)} bars  {df.index[0].date()} -> {df.index[-1].date()}")
print(f"  -> {OUT}")
print(f"\nLatest close {df['close'].iloc[-1]:,.2f}  "
      f"100MA {df['close'].rolling(100).mean().iloc[-1]:,.2f}")
