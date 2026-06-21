import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import requests, io
import pandas as pd

print("Downloading symbol master...")
r = requests.get("https://public.fyers.in/sym_details/NSE_FO.csv", timeout=15)
df = pd.read_csv(io.StringIO(r.text), header=None, low_memory=False)

# Column 9 = Fyers symbol, Column 1 = description
sym_col, desc_col = 9, 1

# Show ALL NIFTY (not BANKNIFTY/FINNIFTY) CE/PE options
nifty = df[
    df[desc_col].astype(str).str.match(r"^NIFTY \d")
    & df[desc_col].astype(str).str.contains("CE|PE")
]

print(f"\nNIFTY options in master: {len(nifty)}")
print("\n=== Sample NIFTY symbols (first 30) ===")
for _, row in nifty.head(30).iterrows():
    print(f"  {row[sym_col]:45s}  |  {row[desc_col]}")

# Find weekly vs monthly — weekly will have a day number in the description
weekly = nifty[nifty[desc_col].astype(str).str.match(r"^NIFTY \d{1,2} \w{3}")]
monthly = nifty[nifty[desc_col].astype(str).str.match(r"^NIFTY \w{3}")]

print(f"\nWeekly (non-monthly) format samples:")
for _, row in weekly.head(5).iterrows():
    print(f"  {row[sym_col]:45s}  |  {row[desc_col]}")

print(f"\nMonthly format samples:")
for _, row in monthly.head(5).iterrows():
    print(f"  {row[sym_col]:45s}  |  {row[desc_col]}")
