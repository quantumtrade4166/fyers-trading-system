"""
mcx_terminal/probe_order_feed.py — does Kotak's ORDER-UPDATE push channel still work? READ-ONLY.

Logs in (Kotak P), opens the SDK's order-feed websocket (wss://<dc>.kotaksecurities.com/realtime),
sends the connect message, and listens --secs seconds. Prints every message with its receive time
(tokens are never printed). Places NO order — to see a real update, place/cancel something by hand
while it listens.

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.probe_order_feed --secs 60
"""
import datetime as dt
import json
import sys
import threading
import time

import websocket

from live_trading_options.mcx_terminal.kotak_session import login

sys.stdout.reconfigure(encoding="utf-8")
SECS = int(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 45
URLS = {None: "wss://mis.kotaksecurities.com/realtime", "adc": "wss://cis.kotaksecurities.com/realtime",
        "e21": "wss://e21.kotaksecurities.com/realtime", "e22": "wss://e22.kotaksecurities.com/realtime",
        "e41": "wss://e41.kotaksecurities.com/realtime", "e43": "wss://e43.kotaksecurities.com/realtime"}


def now():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def main():
    s = login()
    cfg = s.client.configuration
    dc = (getattr(cfg, "data_center", None) or "").lower() or None
    token, sid = cfg.edit_token, cfg.edit_sid
    url = URLS.get(dc, URLS[None])
    print(f"{now()} data center {dc!r} -> {url}")
    secrets = [x for x in (token, sid) if x]

    def clean(m):
        m = m if isinstance(m, str) else m.decode("utf-8", "replace")
        for x in secrets:
            m = m.replace(x, "***")
        return m[:600]

    def on_open(ws):
        msg = "{type:cn,Authorization:%s,Sid:%s,src:WEB}" % (token, sid)
        ws.send(msg)
        print(f"{now()} open, connect sent")

        def hb():
            while True:
                time.sleep(25)
                try:
                    ws.send("{type:hb}")
                except Exception:
                    return
        threading.Thread(target=hb, daemon=True).start()

    ws = websocket.WebSocketApp(url, on_open=on_open,
                                on_message=lambda w, m: print(f"{now()} MSG {clean(m)}"),
                                on_error=lambda w, e: print(f"{now()} ERROR {e}"),
                                on_close=lambda w, c, r: print(f"{now()} CLOSED {c} {r}"))
    threading.Thread(target=lambda: ws.run_forever(ping_interval=20), daemon=True).start()

    # side by side: what fast POLLING of the order book sees, and when (status changes only)
    seen = {}

    def poll():
        while True:
            try:
                r = s.client.order_report()
                for o in (r.get("data") or []) if isinstance(r, dict) else []:
                    key = (o.get("nOrdNo"), o.get("ordSt"), o.get("fldQty"))
                    if key not in seen:
                        seen[key] = now()
                        if len(seen) > 0 and started[0]:
                            print(f"{now()} POLL order {o.get('nOrdNo')} {o.get('trdSym')} {o.get('trnsTp')} "
                                  f"status={o.get('ordSt')} filled={o.get('fldQty')}")
            except Exception as e:
                print(f"{now()} poll error {e}")
            time.sleep(0.1)
    started = [False]
    threading.Thread(target=poll, daemon=True).start()
    time.sleep(2)
    started[0] = True                 # ignore the orders that already existed
    print(f"{now()} listening {SECS}s - place / cancel any order now")
    time.sleep(SECS)
    ws.close()
    print(f"{now()} done")


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stdout.flush()
        import os
        os._exit(0)
