import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from datetime import date, timedelta
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from auth.fyers_auth import get_fyers_client
from options.symbol_gen import make_nifty_symbol

fyers = get_fyers_client()

# ── Step 1: Get current NIFTY spot level ─────────────────────
r = fyers.quotes({"symbols": "NSE:NIFTY50-INDEX"})
print(f"NIFTY quote: {r.get('s')}")
if r.get("s") == "ok":
    lp = r["d"][0]["v"]["lp"]
    print(f"NIFTY current price: {lp}")
    atm = round(lp / 50) * 50
    print(f"ATM strike: {atm}")
else:
    print(f"Quote error: {r}")
    atm = 24500   # fallback

print()

# ── Step 2: Try ATM strike on nearest upcoming expiry ─────────
# June 5, 2026 is next Thursday
next_expiry = date(2026, 6, 5)
sym_ce = make_nifty_symbol(next_expiry, atm, "CE")
sym_pe = make_nifty_symbol(next_expiry, atm, "PE")
print(f"ATM CE: {sym_ce}")
print(f"ATM PE: {sym_pe}")

for sym in [sym_ce, sym_pe]:
    r = fyers.history(data={"symbol": sym, "resolution": "D", "date_format": "1",
        "range_from": "2026-05-25", "range_to": "2026-05-30", "cont_flag": "1"})
    n = len(r.get("candles", []))
    print(f"  {r.get('s'):8s} | {n:3d} bars | {sym} | {r.get('message','')}")
    if n > 0:
        print(f"    sample: {r['candles'][0]}")

print()

# ── Step 3: Try 1-min on today ────────────────────────────────
print("1-min resolution for today:")
r = fyers.history(data={"symbol": sym_ce, "resolution": "1", "date_format": "1",
    "range_from": "2026-05-29", "range_to": "2026-05-30", "cont_flag": "1"})
n = len(r.get("candles", []))
print(f"  {r.get('s'):8s} | {n:3d} 1-min bars | {r.get('message','')}")
if n > 0:
    print(f"  Columns per bar: {len(r['candles'][0])}")
    print(f"  First bar: {r['candles'][0]}")
    print(f"  Last bar:  {r['candles'][-1]}")
