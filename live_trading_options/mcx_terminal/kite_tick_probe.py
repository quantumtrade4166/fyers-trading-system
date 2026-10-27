"""
mcx_terminal/kite_tick_probe.py — the same tick-timing test on Zerodha Kite's MCX feed.

READ-ONLY: reads today's Kite token (deployment/zerodha_token.json — never logs in), downloads the
MCX instrument list, opens ONE KiteTicker (full mode) on the CRUDEOIL nearest-expiry chain around ATM
(+ the future) for --secs seconds, and reports the same numbers as tick_timing_probe.py.
Check first that the VPS has a free Kite websocket slot (Zerodha allows 3 per API key).

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.kite_tick_probe --secs 60
"""
import datetime as dt
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "live_trading_options" / "strangle_strategy"))
from live import kite_executor as kx                          # noqa: E402  (API key + today's token)
from kiteconnect import KiteConnect, KiteTicker              # noqa: E402

SECS = int(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 60


def main():
    kite = KiteConnect(api_key=kx._API_KEY)
    kite.set_access_token(kx.access_token())
    ins = [i for i in kite.instruments("MCX") if i["name"] == "CRUDEOIL"]
    today = dt.date.today()
    opts = [i for i in ins if i["instrument_type"] in ("CE", "PE") and i["expiry"] >= today]
    exp = min(i["expiry"] for i in opts)
    opts = [i for i in opts if i["expiry"] == exp]
    futs = sorted([i for i in ins if i["instrument_type"] == "FUT" and i["expiry"] >= exp], key=lambda i: i["expiry"])
    fut = futs[0]
    ltp = kite.ltp([f"MCX:{fut['tradingsymbol']}"])[f"MCX:{fut['tradingsymbol']}"]["last_price"]
    ks = sorted({i["strike"] for i in opts})
    mid = min(ks, key=lambda k: abs(k - ltp))
    i = ks.index(mid)
    near = set(ks[max(0, i - 10): i + 11])
    toks = [o["instrument_token"] for o in opts if o["strike"] in near] + [fut["instrument_token"]]

    arrivals = defaultdict(list)
    ex_times, lt_times, frames = [], [], []
    t0 = [0.0]
    kws = KiteTicker(kx._API_KEY, kx.access_token())

    def on_ticks(ws, ticks):
        now = time.time() * 1000
        frames.append(now)
        for t in ticks:
            arrivals[t["instrument_token"]].append(now)
            if t.get("exchange_timestamp"):
                ex_times.append(t["exchange_timestamp"])
            if t.get("last_trade_time"):
                lt_times.append(t["last_trade_time"])

    def on_connect(ws, resp):
        t0[0] = time.time() * 1000
        ws.subscribe(toks)
        ws.set_mode(ws.MODE_FULL, toks)

    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.connect(threaded=True)
    time.sleep(SECS)
    kws.close()

    gaps, counts = [], {}
    for tok, ts in arrivals.items():
        ts = [x for x in ts if x - t0[0] > 3000]
        counts[tok] = len(ts)
        gaps += [b - a for a, b in zip(ts, ts[1:])]
    gaps.sort()
    print(f"[kite full] CRUDEOIL {exp} ATM {mid} · {len(toks)} contracts · {SECS}s · {sum(counts.values())} updates "
          f"in {len(frames)} frames ({len(frames) / SECS:.1f} frames/s)")
    if gaps:
        q = lambda f: gaps[int(len(gaps) * f)]
        print(f"gap between two updates of the SAME contract (ms): min {gaps[0]:.0f} · p10 {q(.1):.0f} · "
              f"median {statistics.median(gaps):.0f} · p90 {q(.9):.0f}")
        print(f"updates < 1000 ms apart: {sum(g < 1000 for g in gaps)} of {len(gaps)} "
              f"({100 * sum(g < 1000 for g in gaps) / len(gaps):.0f}%) · < 250 ms: {sum(g < 250 for g in gaps)}")
    print(f"busiest contracts (updates in {SECS - 3}s): {sorted(counts.items(), key=lambda x: -x[1])[:3]}")
    for name, vals in (("exchange_timestamp", ex_times), ("last_trade_time", lt_times)):
        if vals:
            micro = sum(1 for v in vals if getattr(v, "microsecond", 0))
            print(f"kite {name}: sample {vals[-1]} -> {'has sub-second parts' if micro else 'whole seconds'} ({micro} of {len(vals)} with sub-second)")


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stdout.flush()
        import os
        os._exit(0)
