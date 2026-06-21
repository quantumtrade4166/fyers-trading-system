import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from datetime import date
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from auth.fyers_auth import get_fyers_client

fyers = get_fyers_client()

# Use EXACT symbol from the live symbol master: NSE:NIFTY2660219150CE = NIFTY 02 Jun 26 19150 CE
EXACT_SYM = "NSE:NIFTY2660219150CE"

print(f"Symbol: {EXACT_SYM}\n")

# Try 1: with cont_flag=1, string dates
r = fyers.history(data={"symbol": EXACT_SYM, "resolution": "D", "date_format": "1",
    "range_from": "2026-05-20", "range_to": "2026-05-29", "cont_flag": "1"})
print(f"[1] cont_flag=1, date_format=1: {r.get('s')} | {len(r.get('candles',[]))} bars | {r.get('message','')}")

# Try 2: without cont_flag
r = fyers.history(data={"symbol": EXACT_SYM, "resolution": "D", "date_format": "1",
    "range_from": "2026-05-20", "range_to": "2026-05-29"})
print(f"[2] no cont_flag:               {r.get('s')} | {len(r.get('candles',[]))} bars | {r.get('message','')}")

# Try 3: cont_flag=0
r = fyers.history(data={"symbol": EXACT_SYM, "resolution": "D", "date_format": "1",
    "range_from": "2026-05-20", "range_to": "2026-05-29", "cont_flag": "0"})
print(f"[3] cont_flag=0:                {r.get('s')} | {len(r.get('candles',[]))} bars | {r.get('message','')}")

# Try 4: epoch timestamps instead of strings
import time
from_epoch = int(date(2026, 5, 20).strftime("%s") if hasattr(date(2026,5,20),'strftime') else
              (date(2026,5,20) - date(1970,1,1)).total_seconds())
to_epoch = int((date(2026,5,29) - date(1970,1,1)).total_seconds())
r = fyers.history(data={"symbol": EXACT_SYM, "resolution": "D", "date_format": "0",
    "range_from": str(from_epoch), "range_to": str(to_epoch), "cont_flag": "1"})
print(f"[4] date_format=0 (epoch):      {r.get('s')} | {len(r.get('candles',[]))} bars | {r.get('message','')}")

# Try 5: quotes() to check if symbol is valid at all
r = fyers.quotes({"symbols": EXACT_SYM})
print(f"\n[5] quotes(): {r.get('s')} | {r.get('message','')} | {str(r)[:200]}")

# Try 6: Check what methods are available
print(f"\n[6] Fyers client methods: {[m for m in dir(fyers) if not m.startswith('_')]}")
