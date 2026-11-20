"""Screen <-> order gateway, end to end: a real gateway HTTP/WS server (fake Kotak) + the screen's GatewayLink.

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.test_gateway_link
"""
import socket
import sys
import threading
import time
import types

sys.stdout.reconfigure(encoding="utf-8")
from live_trading_options.mcx_terminal import gateway as G
from live_trading_options.mcx_terminal import remote as R
from live_trading_options.mcx_terminal import server as S
from live_trading_options.mcx_terminal.test_gateway import _gw, _tick

FAIL = PASS = 0


def check(name, ok, info=""):
    global FAIL, PASS
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL", name, str(info)[:300])


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _serve(gw, secret, port):
    import uvicorn
    cfg = uvicorn.Config(G.build_app(gw, secret), host="127.0.0.1", port=port, log_level="error",
                         ws_ping_interval=20, ws_ping_timeout=20)
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    return srv, th


def _wait(cond, secs=5.0):
    t = time.time()
    while time.time() - t < secs:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_end_to_end():
    gw, k, ce, pe = _gw("e2e")
    k.instant = "complete"
    port, secret = _free_port(), "e2e-secret"
    srv, th = _serve(gw, secret, port)
    updates = []
    link = R.GatewayLink(port=port, secret=secret, on_update=lambda kind, m: updates.append(kind))
    check("screen link connects to the gateway", _wait(lambda: link.up), link.state)
    live = R.RemoteLive(link)
    check("screen sees the gateway's (empty) book", live.positions() == [] and live.open_positions() == 0)

    # arm through the link, order through the link
    r = link.op("mode_set", {"mode": "LIVE", "_user": "owner@x"})
    check("arm through the gateway", r.get("ok") and gw.arm.get("armed"), r)
    check("screen learns it is LIVE", _wait(lambda: (link.state.get("status") or {}).get("armed") is True), link.state)
    _tick(gw, ce, 500.0)
    t0 = time.time()
    r = link.op("order", {"token": ce.token, "side": "S", "lots": 1, "source": "keyboard"})
    took = time.time() - t0
    check("order placed through the link", r.get("ok"), r)
    check("local hop is fast", took < 1.0, took)
    check("screen gets the fill pushed", _wait(lambda: any(o.get("status") == "COMPLETE" for o in live.snapshot()["orders"])),
          link.state.get("book"))
    gw.live.sync_positions()                                     # MCX: positions come from Kotak's position list
    check("screen gets the new position pushed", _wait(lambda: len(live.positions()) == 1 and live.pos.get(ce.token)),
          link.state.get("book"))

    # the gateway goes away: the screen refuses at once, never hangs
    srv.should_exit = True
    th.join(5)
    check("screen notices the gateway is down", _wait(lambda: not link.up, R.STALE_S + 4), link.connected)
    t0 = time.time()
    r = link.op("exit_all", {})
    check("LIVE action refused at once with ORDERS OFFLINE", not r.get("ok") and "ORDERS OFFLINE" in r.get("msg", "")
          and time.time() - t0 < 0.5, (r, time.time() - t0))
    check("broker shows not readable while down", live.broker.get("ok") is False)

    # it comes back: the link reconnects by itself
    srv2, th2 = _serve(gw, secret, port)
    check("link reconnects when the gateway is back", _wait(lambda: link.up, 8), link.connected)
    srv2.should_exit = True
    link._stop = True
    th2.join(5)


def test_screen_routes_live_only():
    """In LIVE the screen forwards order actions to the gateway; in PAPER it never does."""
    calls = []
    fake_gw = types.SimpleNamespace(op=lambda name, m: calls.append((name, m)) or {"ok": True, "msg": "gw"},
                                    up=True, state={})
    old_engine, old_order = S.ENGINE, S.ORDER_OPS["order"]
    try:
        S.ENGINE = types.SimpleNamespace(gw=fake_gw, mode="LIVE", quote=lambda tok: {"bid": 1.0, "ask": 1.1},
                                         settings=types.SimpleNamespace(get=lambda: {}))
        S.ORDER_OPS["order"] = lambda m: {"ok": True, "msg": "paper"}
        r = S._op_call("order", {"op": "order", "req": 7, "token": 123, "side": "B", "lots": 1})
        check("LIVE order goes to the gateway", r.get("msg") == "gw" and calls and calls[-1][0] == "order", calls)
        check("the screen's quote travels as a hint", calls[-1][1].get("hint") == {"bid": 1.0, "ask": 1.1}, calls[-1])
        check("screen-only fields not forwarded", "req" not in calls[-1][1] and "op" not in calls[-1][1])
        S.ENGINE.mode = "PAPER"
        n = len(calls)
        r = S._op_call("order", {"token": 123, "side": "B", "lots": 1})
        check("PAPER order stays in the screen", r.get("msg") == "paper" and len(calls) == n, calls[n:])
        S.ENGINE.gw = None
        S.ENGINE.mode = "LIVE"
        r = S._op_call("order", {"token": 123, "side": "B", "lots": 1})
        check("gateway off: LIVE order handled as before (in process)", r.get("msg") == "paper")
    finally:
        S.ENGINE, S.ORDER_OPS["order"] = old_engine, old_order


if __name__ == "__main__":
    test_end_to_end()
    test_screen_routes_live_only()
    print(f"{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
