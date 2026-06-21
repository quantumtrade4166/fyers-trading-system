import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from datetime import date
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from auth.fyers_auth import get_fyers_client
from options.symbol_gen import make_nifty_symbol

fyers = get_fyers_client()

tests = [
    (date(2026, 5, 19), 23400, "May 19 (expired 11d ago)"),
    (date(2026, 5, 26), 23400, "May 26 (expired  4d ago)"),
    (date(2026, 6,  2), 23400, "Jun  2 (expires in  3d)"),
    (date(2026, 6,  9), 23400, "Jun  9 (active future) "),
    (date(2026, 6, 16), 23400, "Jun 16 (active future) "),
]

for expiry, strike, label in tests:
    for opt_type in ("CE",):
        sym = make_nifty_symbol(expiry, strike, opt_type)
        r = fyers.history(data={
            "symbol": sym, "resolution": "D", "date_format": "1",
            "range_from": "2026-05-01", "range_to": "2026-05-30", "cont_flag": "1",
        })
        n = len(r.get("candles", []))
        print(f"{label} | {r.get('s'):8s} | {n:3d} bars | {sym} | {r.get('message','')}")
