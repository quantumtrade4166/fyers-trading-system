"""Order gateway (gateway.py) - the LIVE order path in its own process. Fake Kotak, temp data folder.

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.test_gateway
"""
import asyncio
import datetime as dt
import json
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal import gateway as G
from live_trading_options.mcx_terminal import instruments as I
from live_trading_options.mcx_terminal.settings import DEFAULTS
from live_trading_options.mcx_terminal.test_live import FakeKotak, HERE

FAIL = PASS = 0
from live_trading_options.mcx_terminal.risk import CONFIRM_S as C_CONFIRM


def check(name, ok, info=""):
    global FAIL, PASS
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL", name, str(info)[:300])


class _Set:
    def __init__(self, **over):
        self.v = json.loads(json.dumps(DEFAULTS))
        self.v.update(over)

    def get(self):
        return json.loads(json.dumps(self.v))


def _gw(name):
    C.DATA = Path(tempfile.mkdtemp(prefix=f"gw_{name}_"))
    u = I.parse((HERE / "testdata" / "mcx_scrip_sample.csv").read_text(encoding="utf-8"), today="2026-10-19")
    k = FakeKotak()
    gw = G.Gateway(sessions=None, universe=u, client=k, start=False, settings=_Set())
    ch = u.products["CRUDEOIL"].chains["2026-11-17"]
    return gw, k, ch[9300.0]["CE"], ch[9300.0]["PE"]


def _tick(gw, c, ltp, spread=1.0):
    gw.on_ticks([(c.token, ltp, ltp - spread / 2, ltp + spread / 2, 10, 10, None, None, None, None, None, None, None)])


def test_arm_and_prices():
    gw, k, ce, pe = _gw("arm")
    r = gw.op("order", {"token": ce.token, "side": "S", "lots": 1})
    check("new order refused while not armed", not r["ok"] and "not armed" in r["msg"], r)
    check("arm from the screen", gw.set_mode("LIVE", "owner@x")["ok"] and gw.arm["armed"])
    check("arm written to the gateway's own file", (C.DATA / f"live_arm_{C.day()}.json").exists())
    # no own price yet: the screen's hint prices the order
    r = gw.op("order", {"token": ce.token, "side": "S", "lots": 1,
                        "hint": {"ltp": 500.0, "bid": 499.0, "ask": 500.0, "bq": 100, "aq": 100}})
    check("order sent using the screen's quote", r.get("ok") and k.sent and k.sent[-1]["price"] != "0", (r, k.sent[-1:]))
    p1 = float(k.sent[-1]["price"])
    check("sell priced below the hinted bid", p1 < 499.0, p1)
    check("the hinted contract is now watched", ce.token in gw.watch)
    # own fresh feed price beats an older hint
    _tick(gw, pe, 300.0)
    gw.hints[pe.token] = (time.time(), {"ltp": 999.0, "bid": 998.0, "ask": 1000.0})
    q = gw.quote(pe.token)
    check("own fresh feed price used first", q.get("bid") == 299.5, q)
    gw.last_at[pe.token] -= 10                                    # own price goes stale
    check("stale own price -> fresh hint", gw.quote(pe.token).get("bid") == 998.0)
    gw.hints[pe.token] = (time.time() - 5, gw.hints[pe.token][1])
    check("both stale -> last price only, no bid/ask", gw.quote(pe.token).get("bid") is None
          and gw.quote(pe.token).get("ltp") == 300.0)


def test_exits_need_no_arm_and_work():
    gw, k, ce, pe = _gw("exit")
    k.carry[(ce.symbol, "NRML")] = [0, 100, 0.0, 20000.0]          # a short held at Kotak
    k.instant = "complete"
    _tick(gw, ce, 200.0)
    r = gw.op("exit_all", {})
    check("EXIT ALL works even while not armed (exits are always allowed)", r.get("ok") and "FLAT" in r.get("msg", ""), r)
    check("exit sent as a buy", k.sent and k.sent[-1]["transaction_type"] == "B", k.sent[-1:])


def test_timers_and_guard_run_in_gateway():
    gw, k, ce, pe = _gw("timer")
    k.carry[(ce.symbol, "NRML")] = [0, 100, 0.0, 20000.0]
    k.instant = "complete"
    _tick(gw, ce, 200.0)
    gw.live.sync_positions()
    at = (dt.datetime.now(G.IST) + dt.timedelta(minutes=1)).strftime("%H:%M")
    r = gw.op("timer_set", {"name": "CRUDEOIL", "frac": 1.0, "at": at, "_user": "owner@x"})
    check("timer set on the gateway (LIVE)", r.get("ok"), r)
    gw.timers.now = lambda: dt.datetime.now(G.IST) + dt.timedelta(minutes=2)
    n = len(k.sent)
    gw.timers.tick()
    time.sleep(0.3)
    check("timer fired the exit from the gateway", len(k.sent) > n, (n, len(k.sent)))


def test_snapshot_and_push():
    gw, k, ce, pe = _gw("push")
    snap = gw.snapshot()
    check("snapshot has book / risk / timers / status", all(x in snap for x in ("book", "risk", "timers", "status")))

    async def go():
        gw.loop = asyncio.get_running_loop()
        q = asyncio.Queue()
        gw.listeners.add(q)
        gw.on_change("risk")
        gw.on_change("risk")                                     # coalesced
        await asyncio.sleep(0.4)
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        return msgs
    msgs = asyncio.run(go())
    check("change pushed to the screen, bursts coalesced", len(msgs) == 1 and msgs[0]["t"] == "risk", msgs)


async def _asgi(app, method, path, body=None, headers=None):
    sent = []
    msgs = [{"type": "http.request", "body": json.dumps(body or {}).encode(), "more_body": False}]

    async def receive():
        return msgs.pop(0) if msgs else {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)
    hd = [(b"content-type", b"application/json")] + [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": hd, "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 47681), "scheme": "http",
             "http_version": "1.1", "root_path": ""}
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(data or b"{}")


def test_http_secret():
    gw, k, ce, pe = _gw("http")
    app = G.build_app(gw, "s3cret")

    async def go():
        a = await _asgi(app, "POST", "/op/exit_all", {})
        b = await _asgi(app, "POST", "/op/mode_set", {"mode": "LIVE", "_user": "x"}, {G.SECRET_HEADER: "s3cret"})
        c = await _asgi(app, "GET", "/health")
        return a, b, c
    a, b, c = asyncio.run(go())
    check("no secret -> refused", a[0] == 403, a)
    check("with secret -> done", b[0] == 200 and b[1].get("ok"), b)
    check("health needs no secret and tells armed", c[0] == 200 and c[1].get("armed") is True, c)


def test_mtm_guard_fires_from_gateway_ticks():
    """The screen can be frozen: the gateway's own price feed alone must trigger the MTM stop."""
    gw, k, ce, pe = _gw("guard")
    k.carry[(ce.symbol, "NRML")] = [0, 100, 0.0, 20000.0]          # short 100 @ 200
    k.instant = "complete"
    _tick(gw, ce, 200.0)
    gw.live.sync_positions()
    r = gw.op("risk_set", {"stop": -1000})
    check("MTM stop armed on the gateway", r.get("ok"), r)
    n = len(k.sent)
    _tick(gw, ce, 215.0)                                           # -1500 on the short
    time.sleep(C_CONFIRM + 0.2)
    _tick(gw, ce, 215.0)
    time.sleep(0.6)
    check("stop crossed -> exit sent by the gateway", len(k.sent) > n and k.sent[-1]["transaction_type"] == "B",
          (n, k.sent[-1:]))


def test_daily_restart_time():
    born = dt.datetime(2026, 11, 19, 22, 30, tzinfo=G.IST)
    r = G.next_restart(born)
    check("gateway restarts at the next daily reset/refresh", r > born and r.time() in (C.RESET_AT, C.REFRESH_AT), r)


if __name__ == "__main__":
    test_arm_and_prices()
    test_exits_need_no_arm_and_work()
    test_timers_and_guard_run_in_gateway()
    test_snapshot_and_push()
    test_http_secret()
    test_mtm_guard_fires_from_gateway_ticks()
    test_daily_restart_time()
    print(f"{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
