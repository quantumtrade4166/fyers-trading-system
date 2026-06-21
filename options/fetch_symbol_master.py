import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import requests
import io
import pandas as pd
import time

# Fyers publishes the full symbol master as a CSV — this gives us the exact format
URLS = [
    "https://public.fyers.in/sym_details/NSE_FO.csv",
    "https://public.fyers.in/sym_details/NSE_FO_symbols.csv",
]

df = None
for url in URLS:
    print(f"Trying: {url}")
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            print(f"  Downloaded {len(r.content):,} bytes")
            df = pd.read_csv(io.StringIO(r.text), header=None, low_memory=False)
            print(f"  Shape: {df.shape}")
            print(f"  First row: {df.iloc[0].tolist()}")
            break
        else:
            print(f"  HTTP {r.status_code}")
    except Exception as e:
        print(f"  Error: {e}")

if df is None:
    print("\nCould not download symbol master.")
    sys.exit(1)

# Search for NIFTY CE/PE options in the symbol list
# Column 0 or 1 usually has the symbol name
print("\n=== Searching for NIFTY option symbols ===")
for col in range(min(5, len(df.columns))):
    matches = df[df[col].astype(str).str.contains("NIFTY.*CE|NIFTY.*PE", na=False, regex=True)]
    if not matches.empty:
        print(f"\nFound in column {col} — sample rows:")
        print(matches.head(20).to_string())
        break

# Also show raw first few rows to understand structure
print("\n=== First 5 rows (raw) ===")
print(df.head(5).to_string())
