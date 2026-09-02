"""live/kotak_resolve_test.py — verify kotak_executor.resolve() on real strikes. No orders."""
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.kotak_auth import login
from live import kotak_executor as ke

client = login()
TESTS = [
    ("NIFTY", "2026-09-08", 24350, "CE"),
    ("NIFTY", "2026-09-08", 24150, "PE"),
    ("SENSEX", "2026-09-03", 77600, "CE"),
    ("SENSEX", "2026-09-03", 77000, "PE"),
]
for idx, exp, strike, ot in TESTS:
    try:
        r = ke.resolve(client, idx, exp, strike, ot)
        print(f"OK    {idx} {exp} {strike}{ot} -> {r}")
    except Exception as e:
        print(f"FAIL  {idx} {exp} {strike}{ot}: {type(e).__name__}: {e}")
print("\nresolve test done (no orders placed).")
