"""live/kotak_probe2.py — confirm the REAL NIFTY/SENSEX contract format + place_order API. No orders."""
import sys, json, inspect
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live.kotak_auth import login
from neo_api_client import NeoAPI

client = login()
print("\n----- place_order SOURCE -----")
try:
    print(inspect.getsource(NeoAPI.place_order)[:2600])
except Exception as e:
    print("  (no source)", e)

FIELDS = ["pTrdSymbol", "pSymbolName", "pOptionType", "dStrikePrice;", "pExpiryDate",
          "lExpiryDate", "lLotSize", "pSymbol", "dTickSize"]

for idx, seg in [("NIFTY", "nse_fo"), ("SENSEX", "bse_fo")]:
    print(f"\n===== {idx} ({seg}) exact contracts =====")
    try:
        rows = client.search_scrip(exchange_segment=seg, symbol=idx, option_type="CE", strike_price="")
        exact = [r for r in rows if r.get("pSymbolName") == idx
                 and str(r.get("pOptionType", "")).lower() == "ce"]
        exact.sort(key=lambda r: (r.get("lExpiryDate") or 0, float(r.get("dStrikePrice;") or 0)))
        print(f"exact '{idx}' CE rows: {len(exact)} (of {len(rows)} search hits)")
        # nearest expiry, a handful of strikes around the middle
        if exact:
            near_exp = exact[0].get("lExpiryDate")
            near = [r for r in exact if r.get("lExpiryDate") == near_exp]
            mid = len(near) // 2
            for r in near[max(0, mid - 2): mid + 3]:
                print(json.dumps({k: r.get(k) for k in FIELDS}, default=str))
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
