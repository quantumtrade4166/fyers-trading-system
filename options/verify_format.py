import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from datetime import date
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from auth.fyers_auth import get_fyers_client
from options.symbol_gen import make_nifty_symbol

fyers = get_fyers_client()

def check(sym, from_date, to_date, resolution="D"):
    r = fyers.history(data={
        "symbol": sym, "resolution": resolution, "date_format": "1",
        "range_from": str(from_date), "range_to": str(to_date), "cont_flag": "1",
    })
    n = len(r.get("candles", []))
    status = r.get("s")
    msg = r.get("message", "")
    sample = r["candles"][0] if n > 0 else None
    print(f"  {'OK' if status=='ok' else 'ERR':3s} | {n:4d} bars | {sym}")
    if status != "ok":
        print(f"        Error: {msg}")
    if sample:
        print(f"        Sample candle ({len(sample)} cols): {sample}")
    return status == "ok"

print("=== NIFTY options format verification ===\n")

# ── Test 1: Weekly — 22 May 2025 (May=5, day=22) ────────────────────────────
sym = make_nifty_symbol(date(2025, 5, 22), 24500, "CE")
print(f"Test 1 — Weekly 22-May-2025: {sym}")
check(sym, date(2025, 5, 15), date(2025, 5, 22))

print()

# ── Test 2: Monthly — 29 May 2025 (last Thursday of May) ────────────────────
sym2 = make_nifty_symbol(date(2025, 5, 29), 25000, "CE")
print(f"Test 2 — Monthly 29-May-2025: {sym2}")
check(sym2, date(2025, 5, 22), date(2025, 5, 29))

print()

# ── Test 3: How far back does history go? ────────────────────────────────────
sym3 = make_nifty_symbol(date(2024, 6, 27), 23000, "CE")
print(f"Test 3 — Oldest Jun-2024: {sym3}")
check(sym3, date(2024, 6, 1), date(2024, 6, 27))

print()

# ── Test 4: 1-min resolution on recent weekly ────────────────────────────────
sym4 = make_nifty_symbol(date(2025, 5, 22), 24500, "CE")
print(f"Test 4 — 1-min resolution: {sym4}")
check(sym4, date(2025, 5, 21), date(2025, 5, 22), resolution="1")

print()

# ── Test 5: Today's active contract (verify current symbols work) ─────────────
sym5 = make_nifty_symbol(date(2026, 6, 5), 24500, "CE")
print(f"Test 5 — Current contract Jun-5-2026: {sym5}")
check(sym5, date(2026, 5, 29), date(2026, 5, 30))
