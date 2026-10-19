"""
mcx_terminal/probe_kotak_feed.py — Kotak's NEW market feed (native_batch), tick test for MCX options.
=====================================================================================================

Kotak retired the old "HS feed" (wss://mlhsm.kotaksecurities.com) on 15-Sep-2026. The new feed:
  - URL  = data.feedUrl from the login (tradeApiValidate) response, https -> wss
  - auth = first JSON frame {user: UCC, auth: sid, format: "native_batch", source: "NEOTRADEAPI", ...}
  - subscribe = {"event": "subscribeScrips" | "subscribeDepth", "inputtoken": "mcx_fo|123,mcx_fo|456"}
  - data = binary, little-endian, packed, batched; each packet starts with its own uint16 length
Spec: Kotak "Market Data WebSocket — Developer Guide" (native_batch, 2026-09).

Written by hand (not kotakneoapi v3) so the shared VPS venv — used by live Kotak code — is untouched.
READ-ONLY. Places NO order. Never prints the sid, keys or credentials.

    .venv\\Scripts\\python.exe live_trading_options\\mcx_terminal\\probe_kotak_feed.py --secs 60
"""
import datetime as dt
import json
import os
import struct
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import probe_kotak_mcx as P                     # noqa: E402  (login + scrip master + chain pick)

import websocket                                # noqa: E402  (websocket-client)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SECS = int(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 45
STAMP = dt.datetime.now(IST).strftime("%Y%m%d_%H%M%S")
EXCH = {1: "nse_cm", 2: "nse_fo", 3: "cde_fo", 4: "nse_com", 5: "bse_cm", 6: "bse_fo",
        7: "bse_cd", 8: "bse_co", 9: "mcx_fo", 10: "ncd_co"}

# Market Picture (full) fixed body, offsets per the guide §7.3 (whole packet incl. 9-byte header)
MP = struct.Struct("<HHbBBBB I q q q q q I I I I I q I I I I h i I d i I I I I I B I")
MP_FIELDS = ("len", "code", "exch", "level", "res", "seq", "bml", "token", "tbq", "tsq", "vol", "ltt", "lut",
             "open", "close", "high", "low", "ltp", "ltq", "atp", "_ic", "nbuy", "nsell", "_ts", "chgp", "oi",
             "ttv", "chg", "uc", "lc", "yh", "yl", "lot", "prec", "mult")
LITE = struct.Struct("<HHbBBBB I q I q I i i I B I")
LITE_FIELDS = ("len", "code", "exch", "level", "res", "seq", "bml", "token", "ltt", "ltp", "ltq", "close",
               "chgp", "chg", "lot", "prec", "mult")
ROW = struct.Struct("<q i i")
assert MP.size == 144 and LITE.size == 54


def decode(frame: bytes, div: dict):
    """Yield normalised dicts from one binary batch frame."""
    off = 0
    n = len(frame)
    while off + 2 <= n:
        size = struct.unpack_from("<H", frame, off)[0]
        if size < 9 or off + size > n:
            break
        pkt = frame[off: off + size]
        off += size
        code, ex, level = struct.unpack_from("<HbB", pkt, 2)
        d = div.get(EXCH.get(ex, ""), 100)
        if code in (7207, 105, 104):
            yield {"kind": {7207: "index", 105: "status", 104: "cas"}[code], "exch": EXCH.get(ex)}
            continue
        if level == 1 and size >= LITE.size:
            r = dict(zip(LITE_FIELDS, LITE.unpack_from(pkt, 0)))
            yield {"kind": "lite", "token": r["token"], "ltp": r["ltp"] / d, "ltt": r["ltt"], "ltq": r["ltq"]}
        elif level in (2, 4, 8) and size >= MP.size:
            r = dict(zip(MP_FIELDS, MP.unpack_from(pkt, 0)))
            nb, ns = (1, 1) if level == 4 else (r["nbuy"], r["nsell"])
            rows = []
            p = MP.size
            while len(rows) < nb + ns and p + ROW.size <= size:
                q, pr, no = ROW.unpack_from(pkt, p)
                rows.append((pr / d, q, no))
                p += ROW.size
            yield {"kind": "mp", "level": level, "token": r["token"], "ltp": r["ltp"] / d, "ltt": r["ltt"],
                   "lut": r["lut"], "vol": r["vol"], "oi": r["oi"], "ltq": r["ltq"],
                   "bids": rows[:nb], "asks": rows[nb:nb + ns]}


def main():
    now = dt.datetime.now(IST)
    print(f"Kotak NEW feed probe — {now:%a %d-%b %H:%M:%S} IST")
    c = P.kotak_p_login()
    data = (c.validate_resp or {}).get("data") or {}
    feed = data.get("feedUrl")
    sid = data.get("sid")
    print(f"[1] Kotak P login OK · feedUrl {'present' if feed else 'MISSING'} · sid {'present' if sid else 'MISSING'}")
    if not feed:
        print("    login data keys:", sorted(data.keys()))
        return
    wss = "wss://" + feed.split("://", 1)[1] if "://" in feed else feed
    print(f"    feed host: {wss.split('/')[2]}")

    keep, m = P.scrip_master(c)
    toks, depth_toks = [], []
    for name in P.NAMES:
        ref, _ = P.fut_price(c, keep, m, name)
        ch = P.nearest_chain(keep, m, name, ref)
        tt = [x[m["token"]] for x in ch]
        toks += tt
        depth_toks += tt[len(tt) // 2 - 1: len(tt) // 2 + 1]           # two near-ATM symbols
        print(f"    {name:<9} ref={ref}  chain={len(tt)}")

    div = {}
    stats = {"auth": None, "acks": 0, "text": [], "frames": 0, "bytes": 0}
    counts = defaultdict(int)
    first = {}
    ltt_delay, lut_delay = [], []
    kinds = defaultdict(int)
    sample = {}
    depth_seen = {}
    lock = threading.Lock()
    t0 = [None]

    def on_open(ws):
        t0[0] = time.time()
        ws.send(json.dumps({"user": c.ucc, "auth": sid, "format": "native_batch", "source": "NEOTRADEAPI",
                            "sdk_version": 1.1, "sdk_date": "2026-10-19"}))

    def on_message(ws, msg):
        now_s = time.time()
        if isinstance(msg, str):
            try:
                j = json.loads(msg)
            except ValueError:
                stats["text"].append(msg[:200])
                return
            mc = j.get("message_code")
            if mc in (1117, 1119):
                for ex, v in (j.get("exchanges") or {}).items():
                    div[ex] = v.get("divider", 100)
                if mc == 1117 and stats["auth"] is None:
                    stats["auth"] = {"ok": True, "ms": round((now_s - t0[0]) * 1000), "format": j.get("format"),
                                     "dividers": dict(div)}
                    inp = ",".join(f"mcx_fo|{t}" for t in toks)
                    ws.send(json.dumps({"event": "subscribeScrips", "inputtoken": inp, "ack_symbol": True}))
                    ws.send(json.dumps({"event": "subscribeDepth",
                                        "inputtoken": ",".join(f"mcx_fo|{t}" for t in depth_toks)}))
            elif mc == 1120:
                stats["auth"] = {"ok": False, "msg": str(j)[:200]}
            elif mc == 1109:
                stats["acks"] += len(j.get("data") or {})
            else:
                stats["text"].append(str(j)[:200])
            return
        with lock:
            stats["frames"] += 1
            stats["bytes"] += len(msg)
            for t in decode(msg, div):
                kinds[t["kind"]] += 1
                if "token" not in t:
                    continue
                tk = t["token"]
                counts[tk] += 1
                first.setdefault(tk, now_s)
                sample.setdefault(t["kind"], t)
                if t.get("bids"):
                    depth_seen[tk] = (len(t["bids"]), len(t["asks"]))
                for key, bucket in (("ltt", ltt_delay), ("lut", lut_delay)):
                    v = t.get(key)
                    if v and v > 1e9:
                        v = v / 1000 if v > 1e12 else v
                        bucket.append(round((now_s - v) * 1000))

    def on_error(ws, e):
        stats["text"].append(f"error: {str(e)[:200]}")

    ws = websocket.WebSocketApp(wss, on_open=on_open, on_message=on_message, on_error=on_error)
    th = threading.Thread(target=lambda: ws.run_forever(ping_interval=20, ping_timeout=10), daemon=True)
    th.start()
    print(f"[4] connecting + subscribing {len(toks)} MCX option symbols (+{len(depth_toks)} depth), listening {SECS}s ...")
    time.sleep(SECS)
    ws.close()

    def pct(a, q):
        a = sorted(a)
        return a[int(len(a) * q)] if a else None

    total = sum(counts.values())
    res = {
        "feed_host": wss.split("/")[2], "auth": stats["auth"], "acks": stats["acks"],
        "subscribed": len(toks), "ticking": len(counts), "ticks": total,
        "per_sec": round(total / SECS, 1), "frames": stats["frames"], "bytes": stats["bytes"],
        "first_tick_ms": round((min(first.values()) - t0[0]) * 1000) if first and t0[0] else None,
        "kinds": dict(kinds), "depth_levels": depth_seen,
        "ltt_delay_ms_p50": pct(ltt_delay, .5), "ltt_delay_ms_p90": pct(ltt_delay, .9),
        "lut_delay_ms_p50": pct(lut_delay, .5), "lut_delay_ms_p90": pct(lut_delay, .9),
        "busiest": sorted(counts.items(), key=lambda x: -x[1])[:5],
        "text": stats["text"][:6], "sample": sample,
    }
    print(f"    auth: {res['auth']}")
    print(f"    acks {res['acks']} · ticking {res['ticking']}/{res['subscribed']} · {total} ticks ({res['per_sec']}/s) "
          f"in {res['frames']} frames · first tick {res['first_tick_ms']} ms after connect")
    print(f"    packet kinds {res['kinds']} · depth rows (bids, asks) {list(depth_seen.values())[:2]}")
    print(f"    delay vs last_trade_time p50={res['ltt_delay_ms_p50']} p90={res['ltt_delay_ms_p90']} ms · "
          f"vs last_update_time p50={res['lut_delay_ms_p50']} p90={res['lut_delay_ms_p90']} ms")
    print(f"    busiest tokens: {res['busiest']}")
    if res["text"]:
        print(f"    other messages: {res['text'][:3]}")
    print(f"    sample: {json.dumps(sample.get('mp') or sample.get('lite'), default=str)[:400]}")
    (P.LOGS / f"mcx_kfeed_probe_{STAMP}.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stdout.flush()
        os._exit(0)
