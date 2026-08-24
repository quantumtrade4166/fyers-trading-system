"""
tools/test_dual_socket.py — can ONE Fyers token hold TWO WebSockets at once?
============================================================================

Both strategies run their own engine, each with its own Fyers data socket, on the
same account token. If Fyers allows only one connection per token, the second one
to connect silently kicks the first off — and the strategy that loses its feed
stops seeing ticks, which means no MTM stop, no square-off, no adjustments.

That is not something to discover with real money on the line, so this opens two
connections the same way the engines do and watches whether BOTH stay up.

Safe to run any time: it only subscribes to an index quote, never places an order.
Run it OUTSIDE market hours and it still proves the connection behaviour (the
connect/disconnect handshake is what matters, not the ticks).

    python live_trading_options/delta_neutral/tools/test_dual_socket.py
"""

import sys
import time
import threading
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.shared import fyers_client
from fyers_apiv3.FyersWebsocket import data_ws

SYMBOL = "NSE:NIFTY50-INDEX"
WATCH_SECONDS = 45

state = {}
lock = threading.Lock()


def mark(name, event, detail=""):
    with lock:
        state.setdefault(name, []).append((time.strftime("%H:%M:%S"), event, str(detail)[:60]))
        print(f"  [{time.strftime('%H:%M:%S')}] {name:8s} {event:12s} {str(detail)[:60]}", flush=True)


def make_socket(name: str, token: str):
    ws = None

    def on_open():
        mark(name, "CONNECTED")
        try:
            ws.subscribe(symbols=[SYMBOL], data_type="SymbolUpdate")
            mark(name, "SUBSCRIBED", SYMBOL)
        except Exception as e:
            mark(name, "SUB_FAILED", e)

    def on_msg(m):
        with lock:
            n = sum(1 for _, e, _ in state.get(name, []) if e == "TICK")
        if n < 2:
            mark(name, "TICK", m)

    ws = data_ws.FyersDataSocket(
        access_token=token, log_path="", litemode=False, write_to_file=False,
        reconnect=False,                       # no auto-reconnect: we want the TRUTH
        on_connect=on_open,
        on_close=lambda m: mark(name, "CLOSED", m),
        on_error=lambda m: mark(name, "ERROR", m),
        on_message=on_msg)
    return ws


def main():
    st = fyers_client.token_status()
    print(f"  Fyers token date={st['date']} valid={st['valid']}")
    if not st["valid"]:
        print("  token invalid — aborting")
        return
    token = f"{fyers_client.CLIENT_ID}:{fyers_client.load_raw_token()}"

    a = make_socket("SOCKET-A", token)
    b = make_socket("SOCKET-B", token)

    print("\n  opening SOCKET-A (stands in for the VWAP V2 engine)…")
    threading.Thread(target=a.connect, daemon=True).start()
    time.sleep(8)

    print("\n  opening SOCKET-B (stands in for the delta-neutral engine)…")
    threading.Thread(target=b.connect, daemon=True).start()

    print(f"\n  watching both for {WATCH_SECONDS}s — the question is whether A survives B\n")
    time.sleep(WATCH_SECONDS)

    print("\n  ── verdict ──")
    verdict_ok = True
    for name in ("SOCKET-A", "SOCKET-B"):
        evs = state.get(name, [])
        kinds = [e for _, e, _ in evs]
        connected = "CONNECTED" in kinds
        closed_after = False
        if connected:
            ci = kinds.index("CONNECTED")
            closed_after = any(k in ("CLOSED", "ERROR") for k in kinds[ci + 1:])
        status = ("never connected" if not connected
                  else "DROPPED after connecting" if closed_after else "stayed up")
        if not connected or closed_after:
            verdict_ok = False
        print(f"    {name}: {status}   ({len(evs)} events)")

    print()
    if verdict_ok:
        print("  ✅ BOTH sockets held simultaneously on one token.")
        print("     Running the two engines side by side is safe.")
    else:
        print("  ❌ One socket did not survive the other.")
        print("     Do NOT run both engines at once — the delta-neutral engine would")
        print("     knock the live VWAP feed offline. Share one socket instead.")

    try:
        a.close_connection(); b.close_connection()
    except Exception:
        pass


if __name__ == "__main__":
    main()
