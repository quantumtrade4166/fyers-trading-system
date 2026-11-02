"""
cepe_imbalance/probe_index_fields.py — READ-ONLY: map Kotak index packet (7207) fields against a Fyers quote.
One Kotak login, one feed connection (~10 s), ONE Zerodha REST quote. No orders.

    .venv\\Scripts\\python.exe -m live_trading_options.cepe_imbalance.probe_index_fields
"""
import json
import struct
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import websocket

from live_trading_options.mcx_terminal import kotak_session

ROOT = Path(__file__).resolve().parents[2]


def fyers_quote():
    """Reference: Zerodha REST quote (no websocket slot used)."""
    sys.path.insert(0, str(ROOT / "live_trading_options" / "strangle_strategy"))
    from live import kite_executor as kx
    k = kx.get_kite()
    q = k.quote(["NSE:NIFTY 50", "BSE:SENSEX"])
    for n, v in q.items():
        print("KITE", n, "last", v.get("last_price"), "ohlc", v.get("ohlc"), "chg", v.get("net_change"), "ts", v.get("timestamp"))


def main():
    if "--fyers-only" in sys.argv:
        return fyers_quote()
    s = kotak_session.login()
    got = {}
    ev = threading.Event()

    def on_open(ws):
        ws.send(json.dumps({"user": s.ucc, "auth": s.sid, "format": "native_batch",
                            "source": "NEOTRADEAPI", "sdk_version": 1.1, "sdk_date": "2026-10-19"}))

    def on_msg(ws, msg):
        if isinstance(msg, (bytes, bytearray)) and msg[:1] != b"{":
            off = 0
            while off + 2 <= len(msg):
                size = struct.unpack_from("<H", msg, off)[0]
                if size < 9 or off + size > len(msg):
                    break
                code, ex = struct.unpack_from("<Hb", msg, off + 2)
                if code == 7207:
                    got[ex] = bytes(msg[off:off + size])
                off += size
            return
        j = json.loads(msg if isinstance(msg, str) else msg.decode())
        if j.get("message_code") == 1117:
            ws.send(json.dumps({"event": "subscribeScrips", "inputtoken": "nse_cm|Nifty 50,bse_cm|SENSEX"}))
            ev.set()

    ws = websocket.WebSocketApp(s.feed_url, on_open=on_open, on_message=on_msg)
    threading.Thread(target=ws.run_forever, daemon=True).start()
    ev.wait(15)
    time.sleep(8)
    ws.close()
    for ex, p in got.items():
        name = p[65:].rstrip(b"\x00 ").decode(errors="replace")
        ints = [struct.unpack_from("<i", p, o)[0] for o in range(13, 61, 4)]
        print("KOTAK", ex, name, "int32 @13..57:", ints, "hex:", p.hex())
    fyers_quote()


if __name__ == "__main__":
    main()
