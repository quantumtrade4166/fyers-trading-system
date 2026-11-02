"""
cepe_imbalance/probe_kotak_universe.py — READ-ONLY probe for the CE/PE imbalance paper engine.
==============================================================================================

Checks, on the VPS, with the Kotak P login (same as the MCX terminal; a 2nd login does not
end other sessions):
  1. scrip masters nse_fo / bse_fo / mcx_fo: header, encodings, sample rows for NIFTY, SENSEX,
     CRUDEOIL, GOLD, GOLDM, SILVER, SILVERM; trimmed samples saved to testdata/
  2. the new feed: index subscription (NIFTY 50 / SENSEX spot) — which event + token form works —
     and that nse_fo / bse_fo / mcx_fo option ticks all arrive on ONE connection.
     Raw hex of index packets (code 7207) is dumped so the layout can be decoded.

Places NO order. Never prints sid, keys or credentials.

    .venv\\Scripts\\python.exe -m live_trading_options.cepe_imbalance.probe_kotak_universe --secs 25
"""
import csv
import datetime as dt
import io
import json
import struct
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import requests
import websocket

from live_trading_options.mcx_terminal import kotak_session
from live_trading_options.mcx_terminal.feed import decode, EXCH

HERE = Path(__file__).resolve().parent
OUT = HERE / "testdata"
OUT.mkdir(exist_ok=True)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SECS = int(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 25
NAMES = {"nse_fo": ("NIFTY",), "bse_fo": ("SENSEX",),
         "mcx_fo": ("CRUDEOIL", "GOLD", "GOLDM", "SILVER", "SILVERM")}
if "--seg" in sys.argv:
    NAMES = {k: v for k, v in NAMES.items() if k == sys.argv[sys.argv.index("--seg") + 1]}


def master(client, seg):
    url = client.scrip_master(exchange_segment=seg)
    if not isinstance(url, str) or not url.startswith("http"):
        raise RuntimeError(f"{seg}: scrip_master gave no URL: {str(url)[:200]}")
    return requests.get(url, timeout=90).text


def main():
    print(f"probe start {dt.datetime.now(IST):%Y-%m-%d %H:%M:%S} IST")
    s = kotak_session.login()
    print("[1] Kotak P login OK")
    pick = {}                                     # seg -> [tokens to subscribe]
    for seg, names in NAMES.items():
        text = master(s.client, seg)
        rows = list(csv.DictReader(io.StringIO(text)))
        hdr = list(rows[0].keys()) if rows else []
        print(f"\n[2] {seg}: {len(rows)} rows; columns: {[h.strip() for h in hdr]}")
        keep = []
        for name in names:
            mine = [{(k or '').strip(): (v or '').strip() for k, v in r.items()} for r in rows
                    if (r.get("pSymbolName") or "").strip() == name]
            kinds = Counter(r.get("pInstType") for r in mine)
            exps = sorted({(r.get("pExpiryDate"), r.get("lExpiryDate")) for r in mine if (r.get("pInstType", "").startswith("OPT") or r.get("pInstType") == "IO")},
                          key=lambda x: int(float(x[1] or 0)))
            print(f"    {name}: {len(mine)} rows, inst types {dict(kinds)}; {len(exps)} option expiries; first 4 (pExpiryDate, lExpiryDate): {exps[:4]}")
            opts = [r for r in mine if (r.get("pInstType", "").startswith("OPT") or r.get("pInstType") == "IO")]
            futs = [r for r in mine if (r.get("pInstType", "").startswith("FUT") or r.get("pInstType") == "IF")]
            for r in (opts[:1] + futs[:1]):
                print("      sample:", {k: r.get(k) for k in ("pSymbol", "pTrdSymbol", "pInstType", "pOptionType", "dStrikePrice;",
                                                           "pExpiryDate", "lExpiryDate", "lLotSize", "dTickSize", "lFreezeQty",
                                                           "dGenNum", "dGenDen", "dPriceNum", "dPriceDen", "pExchSeg", "pSegment")})
            # trimmed sample: nearest 2 option expiries + all futures
            near = {e[1] for e in exps[:2]}
            keep += [r for r in mine if r.get("lExpiryDate") in near or (r.get("pInstType", "").startswith("FUT") or r.get("pInstType") == "IF")]
            if opts and exps:
                e0 = exps[0][1]
                o0 = sorted([r for r in opts if r.get("lExpiryDate") == e0], key=lambda r: float(r.get("dStrikePrice;") or 0))
                mid = o0[len(o0) // 2]
                pick.setdefault(seg, []).append(int(mid["pSymbol"]))
            if futs:
                pick.setdefault(seg, []).append(int(sorted(futs, key=lambda r: int(float(r.get("lExpiryDate") or 0)))[0]["pSymbol"]))
        if keep:
            with open(OUT / f"scrip_{seg}_sample.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(keep[0].keys()))
                w.writeheader()
                w.writerows(keep)
            print(f"    saved {len(keep)} rows -> testdata/scrip_{seg}_sample.csv")

    # ── feed ──────────────────────────────────────────────────────────────
    print(f"\n[3] feed test {SECS}s; scrip tokens: {pick}")
    div = {}
    seen = Counter()
    by_ex_tok = defaultdict(int)
    idx_raw = []
    ctrl = []
    authed = threading.Event()

    def on_open(ws):
        ws.send(json.dumps({"user": s.ucc, "auth": s.sid, "format": "native_batch",
                            "source": "NEOTRADEAPI", "sdk_version": 1.1, "sdk_date": "2026-10-19"}))

    def on_msg(ws, msg):
        if isinstance(msg, (bytes, bytearray)) and msg[:1] == b"{":
            msg = msg.decode("utf-8", "replace")
        if isinstance(msg, (bytes, bytearray)):
            off = 0
            while off + 2 <= len(msg):
                size = struct.unpack_from("<H", msg, off)[0]
                if size < 9 or off + size > len(msg):
                    break
                code, ex, level = struct.unpack_from("<HbB", msg, off + 2)
                seen[(code, EXCH.get(ex), level)] += 1
                if code == 7207 and len(idx_raw) < 6:
                    idx_raw.append((EXCH.get(ex), size, msg[off:off + size].hex()))
                off += size
            for t in decode(msg, div):
                by_ex_tok[t[0]] += 1
            return
        try:
            j = json.loads(msg)
        except ValueError:
            ctrl.append(str(msg)[:200]); return
        ctrl.append({k: v for k, v in j.items() if k != "exchanges"})
        if j.get("message_code") in (1117, 1119):
            for ex, v in (j.get("exchanges") or {}).items():
                div[ex] = v.get("divider", 100) or 100
            if j.get("message_code") == 1117:
                ctrl.append({"dividers": dict(div)})
                authed.set()

    ws = websocket.WebSocketApp(s.feed_url, on_open=on_open, on_message=on_msg)
    threading.Thread(target=lambda: ws.run_forever(ping_interval=15, ping_timeout=8), daemon=True).start()
    if not authed.wait(15):
        print("    feed did not auth in 15 s; control msgs:", ctrl)
        return
    toks = ",".join(f"{seg}|{t}" for seg, ts in pick.items() for t in ts)
    ws.send(json.dumps({"event": "subscribeScrips", "inputtoken": toks}))
    for ev, it in (("subscribeIndex", "nse_cm|Nifty 50,bse_cm|SENSEX"),
                   ("subscribeIndex", "nse_cm|NIFTY 50"),
                   ("subscribeIndex", "nse_cm|26000,bse_cm|1"),
                   ("subscribeScrips", "nse_cm|Nifty 50,bse_cm|SENSEX")):
        time.sleep(1.5)
        ws.send(json.dumps({"event": ev, "inputtoken": it}))
        ctrl.append(f">> sent {ev} {it}")
    time.sleep(SECS)
    ws.close()
    print("    control msgs:")
    for c in ctrl:
        print("      ", c)
    print("    packet kinds (code, exch, level): count")
    for k, v in sorted(seen.items(), key=lambda x: -x[1]):
        print("      ", k, v)
    print("    decoded ticks per token:", dict(by_ex_tok))
    print("    index packets raw (exch, size, hex):")
    for r in idx_raw:
        print("      ", r)


if __name__ == "__main__":
    main()
