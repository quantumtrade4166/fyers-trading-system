"""
mcx_terminal/probe_fyers_mcx.py — Stage 0 (fallback): can Fyers feed an MCX option chain tick by tick?
====================================================================================================

READ-ONLY. Never logs in (reads the token file the VPS writes at 09:00), never trades.

For CRUDEOIL / GOLDM / SILVERM: nearest option expiry, ATM +/- N strikes (+ the near future),
subscribe Fyers SymbolUpdate for --secs seconds, count ticks per symbol and measure the delay
between the exchange time on the tick and arrival on this machine.

    .venv\\Scripts\\python.exe live_trading_options\\mcx_terminal\\probe_fyers_mcx.py --secs 60
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import requests
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

ROOT = Path(__file__).resolve().parents[2]
CLIENT_ID = "W09OMXQB8J-100"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NAMES = ("CRUDEOIL", "GOLDM", "SILVERM")
EACH_SIDE = 10
SECS = int(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 45
STAMP = dt.datetime.now(IST).strftime("%Y%m%d_%H%M%S")
OUT = {"stamp": STAMP}


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def symbols():
    """MCX_COM.csv (no header): 8 expiry epoch, 9 ticker, 13 underlying, 15 strike, 16 CE/PE/XX."""
    txt = requests.get("https://public.fyers.in/sym_details/MCX_COM.csv", timeout=60).text
    rows = list(csv.reader(io.StringIO(txt)))
    return rows


def chain(rows, fy, name):
    now = time.time()
    mine = [r for r in rows if len(r) > 16 and r[13] == name and (fnum(r[8]) or 0) > now]
    futs = sorted((r for r in mine if r[16] == "XX"), key=lambda r: float(r[8]))
    opts = [r for r in mine if r[16] in ("CE", "PE")]
    if not futs or not opts:
        return [], None
    q = fy.quotes({"symbols": futs[0][9]})
    ref = fnum(((q.get("d") or [{}])[0].get("v") or {}).get("lp"))
    e0 = min(float(r[8]) for r in opts)
    opts = [r for r in opts if float(r[8]) == e0]
    strikes = sorted({float(r[15]) for r in opts})
    mid = min(strikes, key=lambda s: abs(s - ref)) if ref else strikes[len(strikes) // 2]
    i = strikes.index(mid)
    want = set(strikes[max(0, i - EACH_SIDE): i + EACH_SIDE + 1])
    syms = [futs[0][9]] + [r[9] for r in opts if float(r[15]) in want]
    return syms, ref


def main():
    n = dt.datetime.now(IST)
    print(f"Fyers MCX probe — {n:%a %d-%b %H:%M:%S} IST")
    tok = json.loads((ROOT / "config" / "access_token.txt").read_text(encoding="utf-8"))["token"]
    fy = fyersModel.FyersModel(client_id=CLIENT_ID, token=tok, is_async=False, log_path="")
    rows = symbols()
    allsyms = []
    for name in NAMES:
        s, ref = chain(rows, fy, name)
        allsyms += s
        print(f"  {name:<9} future ref={ref}  symbols={len(s)}  e.g. {s[1] if len(s) > 1 else s}")
    OUT["symbols"] = len(allsyms)

    counts = defaultdict(int)
    delays = []
    first = {}
    samples = []
    errors = []
    lock = threading.Lock()
    t0 = time.time()

    def on_message(msg):
        now = time.time()
        ticks = msg.get("d") if isinstance(msg, dict) and isinstance(msg.get("d"), list) else [msg]
        with lock:
            for t in ticks:
                if not isinstance(t, dict) or "symbol" not in t:
                    continue
                if len(samples) < 3:
                    samples.append(t)
                s = t["symbol"]
                counts[s] += 1
                first.setdefault(s, now)
                ft = fnum(t.get("exch_feed_time")) or fnum(t.get("last_traded_time"))
                if ft and ft > 1e9:
                    delays.append(round((now - ft) * 1000))

    bad = []

    def on_open():
        # one bad symbol fails a whole batch (code 11011), so subscribe one at a time and
        # note which one an error follows
        def run():
            for s in allsyms:
                before = len(errors)
                ws.subscribe(symbols=[s], data_type="SymbolUpdate")
                time.sleep(0.15)
                if len(errors) > before:
                    bad.append(s)
        threading.Thread(target=run, daemon=True).start()

    ws = data_ws.FyersDataSocket(access_token=f"{CLIENT_ID}:{tok}", log_path="", litemode=False,
                                 write_to_file=False, reconnect=False, on_connect=on_open,
                                 on_close=lambda m: None, on_error=lambda m: errors.append(str(m)[:200]),
                                 on_message=on_message)
    threading.Thread(target=ws.connect, daemon=True).start()
    print(f"  listening {SECS}s on {len(allsyms)} symbols ...")
    time.sleep(SECS)

    # the first message per symbol is a snapshot, not a live tick -> drop it from the delay stats
    total = sum(counts.values())
    d = sorted(delays)
    res = {
        "secs": SECS, "symbols": len(allsyms), "ticking": len(counts), "ticks": total,
        "per_sec": round(total / SECS, 1),
        "first_after_ms": round((min(first.values()) - t0) * 1000) if first else None,
        "delay_ms_p50": d[len(d) // 2] if d else None,
        "delay_ms_p90": d[int(len(d) * 0.9)] if d else None,
        "busiest": sorted(counts.items(), key=lambda x: -x[1])[:5],
        "errors": errors[:5], "sample": samples[:1], "rejected_symbols": bad,
        "silent_symbols": [s for s in allsyms if s not in counts and s not in bad],
    }
    OUT.update(res)
    print(f"  ticking {res['ticking']}/{res['symbols']} symbols, {total} ticks ({res['per_sec']}/s), "
          f"first after {res['first_after_ms']} ms")
    print(f"  exchange->VPS delay p50={res['delay_ms_p50']} ms  p90={res['delay_ms_p90']} ms  "
          f"(includes the 1-second resolution of the exchange time stamp)")
    print(f"  busiest: {res['busiest']}")
    print(f"  rejected by Fyers ({len(res['rejected_symbols'])}): {res['rejected_symbols'][:10]}")
    print(f"  subscribed but no tick ({len(res['silent_symbols'])}): {res['silent_symbols'][:10]}")
    if errors:
        print("  errors:", errors[:3])
    (ROOT / "logs" / f"mcx_fyers_probe_{STAMP}.json").write_text(json.dumps(OUT, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stdout.flush()
        os._exit(0)
