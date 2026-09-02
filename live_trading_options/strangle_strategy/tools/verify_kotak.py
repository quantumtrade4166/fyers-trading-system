"""tools/verify_kotak.py — confirm the Kotak wiring imports + login+resolve. No orders."""
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.kotak_controller import KotakController          # engine imports
from live import kotak_executor as ke
print("OK  kotak_controller + kotak_executor import")

from live.kotak_auth import login
c = login()
for idx, exp, strike, ot in [("NIFTY", "2026-09-08", 24350, "CE"),
                             ("SENSEX", "2026-09-04", 81000, "PE")]:
    try:
        r = ke.resolve(c, idx, exp, strike, ot)
        print(f"OK  resolve {idx} {strike}{ot} -> {r['trading_symbol']} (lot {r['lot_size']})")
    except Exception as e:
        print(f"FAIL resolve {idx} {strike}{ot}: {e}")
print("verify done (no orders).")
