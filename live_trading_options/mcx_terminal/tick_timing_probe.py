"""
mcx_terminal/tick_timing_probe.py — how "tick-by-tick" is Kotak's MCX feed, measured.

READ-ONLY (own Kotak P session, no orders). Subscribes the CRUDEOIL nearest-expiry chain around ATM
(+ the future) for --secs seconds and reports:
  * arrival precision: our receive stamp (ms) per packet
  * exchange time precision: are last_trade_time / last_update_time whole seconds or milliseconds?
  * update gaps per contract: how often one contract updates (min / median / p10), and how many
    updates come less than 1 s apart (a 1-second snapshot feed would show none)

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.tick_timing_probe --secs 60
"""
import json
import statistics
import struct
import sys
import threading
import time
from collections import defaultdict

import websocket

from live_trading_options.mcx_terminal import feed as F
from live_trading_options.mcx_terminal import instruments as I
from live_trading_options.mcx_terminal.kotak_session import login

sys.stdout.reconfigure(encoding="utf-8")
SECS = int(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 60
EVENT = sys.argv[sys.argv.index("--event") + 1] if "--event" in sys.argv else "subscribeScrips"   # / subscribeScripsLite / subscribeDepth


def main():
    s = login()
    u = I.load(s.client)
    p = u.products["CRUDEOIL"]
    exp = p.expiries()[0]
    q = s.client.quotes(instrument_tokens=[{"instrument_token": str(p.underlying(exp).token), "exchange_segment": "mcx_fo"}],
                        quote_type="ltp")
    try:
        fut = float((q[0] if isinstance(q, list) else q["message"][0])["ltp"])
    except Exception:
        fut = None
    ks = sorted(p.chains[exp])
    mid = min(ks, key=lambda k: abs(k - fut)) if fut else ks[len(ks) // 2]
    i = ks.index(mid)
    near = ks[max(0, i - 10): i + 11]
    toks = [p.chains[exp][k][o].token for k in near for o in ("CE", "PE")] + [p.underlying(exp).token]
    arrivals = defaultdict(list)            # tok -> [receive_ms]
    raw_times = []                          # (ltt, lut) raw integers from packets
    frames = []
    div = {}
    t0 = [0]

    def on_open(ws):
        ws.send(json.dumps({"user": s.ucc, "auth": s.sid, "format": "native_batch", "source": "NEOTRADEAPI",
                            "sdk_version": 1.1, "sdk_date": "2026-10-26"}))

    def on_msg(ws, msg):
        now = time.time() * 1000
        if isinstance(msg, (bytes, bytearray)) and msg[:1] == b"{":
            msg = msg.decode()
        if isinstance(msg, str):
            j = json.loads(msg)
            if j.get("message_code") in (1117, 1119):
                for ex, v in (j.get("exchanges") or {}).items():
                    div[ex] = v.get("divider", 100)
                if j.get("message_code") == 1117:
                    t0[0] = now
                    ws.send(json.dumps({"event": EVENT,
                                        "inputtoken": ",".join(f"mcx_fo|{t}" for t in toks)}))
            return
        frames.append(now)
        off = 0
        while off + 2 <= len(msg):
            size = struct.unpack_from("<H", msg, off)[0]
            if size < 9 or off + size > len(msg):
                break
            code, ex, level = struct.unpack_from("<HbB", msg, off + 2)
            if code == 7208 and level in (2, 4, 8) and size >= F.MP.size:
                f = F.MP.unpack_from(msg, off)
                arrivals[f[7]].append(now)
                raw_times.append((f[11], f[12]))
            elif code == 7208 and level == 1 and size >= F.LITE.size:
                f = F.LITE.unpack_from(msg, off)
                arrivals[f[7]].append(now)
                raw_times.append((f[8], None))
            off += size

    ws = websocket.WebSocketApp(s.feed_url, on_open=on_open, on_message=on_msg)
    threading.Thread(target=lambda: ws.run_forever(ping_interval=15), daemon=True).start()
    time.sleep(SECS)
    ws.close()

    # skip the first 3 s (initial snapshot burst)
    gaps, counts = [], {}
    for tok, ts in arrivals.items():
        ts = [x for x in ts if x - t0[0] > 3000]
        counts[tok] = len(ts)
        gaps += [b - a for a, b in zip(ts, ts[1:])]
    gaps.sort()
    ltt = [a for a, b in raw_times if a]
    lut = [b for a, b in raw_times if b]
    print(f"[{EVENT}] CRUDEOIL {exp} ATM {mid} · {len(toks)} contracts · {SECS}s · {sum(counts.values())} updates "
          f"in {len(frames)} frames ({len(frames) / SECS:.1f} frames/s)")
    if gaps:
        q = lambda f: gaps[int(len(gaps) * f)]
        print(f"gap between two updates of the SAME contract (ms): min {gaps[0]:.0f} · p10 {q(.1):.0f} · "
              f"median {statistics.median(gaps):.0f} · p90 {q(.9):.0f}")
        print(f"updates < 1000 ms apart: {sum(g < 1000 for g in gaps)} of {len(gaps)} "
              f"({100 * sum(g < 1000 for g in gaps) / len(gaps):.0f}%) · < 250 ms: {sum(g < 250 for g in gaps)}")
    busiest = sorted(counts.items(), key=lambda x: -x[1])[:3]
    print(f"busiest contracts (updates in {SECS - 3}s): {busiest}")
    for name, vals in (("last_trade_time", ltt), ("last_update_time", lut)):
        if vals:
            mx = max(vals)
            unit = "milliseconds" if mx > 1e12 else "seconds"
            frac = sum(1 for v in vals if v % 1000) if mx > 1e12 else None
            print(f"exchange {name}: sample {vals[-1]} -> whole {unit}" + (f" ({frac} with ms parts)" if frac is not None else ""))
    print("our receive stamp: milliseconds (time.time() * 1000) on every packet")


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stdout.flush()
        import os
        os._exit(0)
