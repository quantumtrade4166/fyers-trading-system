import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import requests, io
import pandas as pd
from datetime import date
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from auth.fyers_auth import get_fyers_client
from options.symbol_gen import make_nifty_symbol

fyers = get_fyers_client()

# ── Step 1: Find ATM NIFTY strikes from symbol master ────────
print("Fetching symbol master...")
r = requests.get("https://public.fyers.in/sym_details/NSE_FO.csv", timeout=15)
df = pd.read_csv(io.StringIO(r.text), header=None, low_memory=False)

# col 9 = symbol, col 1 = description, col 15 = strike
nifty_opts = df[df[1].astype(str).str.match(r"^NIFTY \d")
                & df[1].astype(str).str.contains(" CE| PE")]

print(f"NIFTY options in master: {len(nifty_opts)}")

# ── Step 2: Show all unique expiry dates ──────────────────────
print("\nAll NIFTY expiry dates in master (via symbol descriptions):")
expiries_seen = set()
for _, row in nifty_opts.iterrows():
    desc = str(row[1])   # e.g. "NIFTY 02 Jun 26 23550 CE"
    parts = desc.split()
    if len(parts) >= 4:
        expiry_str = f"{parts[1]} {parts[2]} {parts[3]}"
        expiries_seen.add(expiry_str)
for e in sorted(expiries_seen):
    print(f"  {e}")

# ── Step 3: Find ATM strikes for nearest expiry ───────────────
current_nifty = 23547.75   # from earlier quote
atm = round(current_nifty / 50) * 50

print(f"\nLooking for ATM (~{atm}) NIFTY options in master:")
atm_rows = nifty_opts[
    (nifty_opts[15].astype(float) >= atm - 200) &
    (nifty_opts[15].astype(float) <= atm + 200)
]
for _, row in atm_rows.head(10).iterrows():
    print(f"  {row[9]:45s}  |  {row[1]}")

print()

# ── Step 4: Test exact ATM symbol from master ─────────────────
if not atm_rows.empty:
    test_sym = atm_rows.iloc[0][9]
    print(f"Testing exact ATM symbol from master: {test_sym}")
    r2 = fyers.history(data={"symbol": test_sym, "resolution": "D",
        "date_format": "1", "range_from": "2026-05-25", "range_to": "2026-05-30",
        "cont_flag": "1"})
    n = len(r2.get("candles", []))
    print(f"  Status: {r2.get('s')} | {n} bars | {r2.get('message','')}")
    if n > 0:
        print(f"  Sample: {r2['candles'][0]}")
