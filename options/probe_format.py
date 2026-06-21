import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from datetime import date
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from auth.fyers_auth import get_fyers_client

fyers = get_fyers_client()

def test(sym):
    r = fyers.history(data={
        "symbol": sym, "resolution": "D", "date_format": "1",
        "range_from": "2025-05-15", "range_to": "2025-05-29", "cont_flag": "1",
    })
    status = r.get("s")
    n = len(r.get("candles", []))
    msg = r.get("message", "")
    print(f"  {'OK ' if status=='ok' else 'ERR'} | {n:3d} bars | {sym}")
    if status == "ok" and n > 0:
        print(f"       *** CORRECT FORMAT FOUND *** sample={r['candles'][0]}")
    return status == "ok"

print("=== Probing NIFTY option symbol formats ===\n")

# Strike and expiry to test: 24500 CE, expiry 29 May 2025 (monthly) or 22 May 2025 (weekly)
candidates = [
    # Current attempt (failing)
    "NSE:NIFTY25MAY24500CE-INDEX",
    "NSE:NIFTY2522MAY24500CE-INDEX",

    # Without suffix
    "NSE:NIFTY25MAY24500CE",
    "NSE:NIFTY2522MAY24500CE",

    # Different suffix
    "NSE:NIFTY25MAY24500CE-OPT",
    "NSE:NIFTY2522MAY24500CE-OPT",

    # Full 4-digit year
    "NSE:NIFTY2025MAY24500CE-INDEX",
    "NSE:NIFTY202505224500CE-INDEX",

    # YYMMDD (numeric month)
    "NSE:NIFTY250529 24500CE-INDEX".replace(" ", ""),
    "NSE:NIFTY25052924500CE-INDEX",
    "NSE:NIFTY25052224500CE-INDEX",

    # Strike with decimals
    "NSE:NIFTY25MAY24500.00CE-INDEX",

    # Different month encoding
    "NSE:NIFTY2505 24500CE-INDEX".replace(" ", ""),
    "NSE:NIFTY250524500CE-INDEX",

    # Lowercase CE
    "NSE:NIFTY25MAY24500ce-INDEX",

    # BANKNIFTY for comparison
    "NSE:BANKNIFTY25MAY53000CE-INDEX",
    "NSE:BANKNIFTY2522MAY53000CE-INDEX",

    # Try quotes instead of history to see if it gives a hint
]

found = False
for sym in candidates:
    if test(sym):
        found = True

print()
if not found:
    print("No format matched. Trying symbol search / quotes...")
    # Try to get a quote to see if it gives any info
    r = fyers.quotes({"symbols": "NSE:NIFTY25MAY24500CE-INDEX"})
    print(f"quotes() response: {r}")

    # Try optionchain if available
    try:
        r2 = fyers.optionchain(data={"symbol": "NSE:NIFTY50-INDEX", "strikecount": 2, "timestamp": ""})
        print(f"\noptionchain() response keys: {list(r2.keys())}")
        if r2.get("s") == "ok":
            data = r2.get("data", {})
            print(f"optionchain data keys: {list(data.keys())}")
            # Print a few symbols from the chain
            for key in ["expiryData", "optionsChain"]:
                if key in data:
                    print(f"\n{key} sample: {str(data[key])[:500]}")
    except Exception as e:
        print(f"optionchain error: {e}")
