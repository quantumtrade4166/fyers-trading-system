"""
mcx_terminal/test_algo.py — the Algo tab's narrow proxy (algo.py): only the four SYN books, arming LIVE owner-only.

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.test_algo
"""
import json

from fastapi import FastAPI

from live_trading_options.mcx_terminal import algo


def test_allowed():
    ok = lambda m, p: algo._allowed(m, p.split("/"))[0]
    assert ok("GET", "state") and ok("GET", "days") and ok("GET", "day/2026-11-23") and ok("GET", "day/live")
    for b in ("NIFTY_SYN", "SENSEX_SYN", "NIFTY_SYN_LIVE", "sensex_syn_live", "NIFTY_SYN2", "SENSEX_SYN2_LIVE"):
        assert ok("GET", f"history/{b}") and ok("GET", f"positions/{b}") and ok("POST", f"control/{b}")
        assert ok("POST", f"params/{b}") and ok("POST", f"auto/{b}")
    # nothing of the CE/PE books
    for b in ("NIFTY", "NIFTY_LIVE", "SENSEX_ST", "CRUDEOIL", "BTC_NR"):
        assert not ok("GET", f"history/{b}") and not ok("POST", f"control/{b}"), b
    assert not ok("POST", "live/NIFTY_LIVE/arm") and ok("POST", "live/NIFTY_SYN_LIVE/arm") and ok("POST", "live/SENSEX_SYN2_LIVE/arm")
    assert not ok("GET", "history/NIFTY_SYN3")
    assert not ok("POST", "live/NIFTY_SYN/arm")                   # paper book has no arm
    assert not ok("POST", "reset/NIFTY_SYN_LIVE") and ok("POST", "reset/NIFTY_SYN")
    assert not ok("POST", "order/NIFTY_SYN_LIVE")
    assert not ok("GET", "debug/stacks") and not ok("GET", "day/../x")


def test_proxy_filters_and_owner_arm(monkeypatch):
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request

    import uvicorn

    calls = []

    def fake(method, path, qs, body):
        calls.append((method, path, body))
        if path == "state":
            return 200, json.dumps({"underlyings": [{"name": "NIFTY"}, {"name": "NIFTY_SYN"}, {"name": "SENSEX_SYN_LIVE"}]}).encode()
        return 200, b'{"ok": true}'
    monkeypatch.setattr(algo, "_fetch", fake)
    app = FastAPI()

    @app.middleware("http")
    async def who(request, call_next):
        request.scope["mcx_user"] = request.headers.get("x-user", "local")
        return await call_next(request)
    algo.register(app, lambda: {"local", "owner@x"})
    with socket.socket() as s_:
        s_.bind(("127.0.0.1", 0))
        port = s_.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)

    def call(path, body=None, user=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"x-user": user} if user else {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    try:
        st, raw = call("/algo/api/state")
        assert st == 200 and [u["name"] for u in json.loads(raw)["underlyings"]] == ["NIFTY_SYN", "SENSEX_SYN_LIVE"]
        assert call("/algo/api/history/NIFTY")[0] == 403
        arm = {"armed": True, "confirm": "LIVE"}
        assert call("/algo/api/live/NIFTY_SYN_LIVE/arm", arm, user="brother@x")[0] == 403
        assert call("/algo/api/live/NIFTY_SYN_LIVE/arm", arm, user="owner@x")[0] == 200
        assert json.loads(calls[-1][2])["by"] == "owner@x"
        assert call("/algo")[0] == 200
    finally:
        srv.should_exit = True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
