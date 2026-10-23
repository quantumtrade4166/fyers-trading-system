"""
Offline tests for the tick bus — NO broker contact.

A FakeTicker stands in for the real KiteTicker and lets the test push synthetic ticks.
These prove the properties the LIVE system depends on:
  1. union subscription pushed to the single real ticker (highest mode wins per token)
  2. correct per-consumer fan-out (each consumer gets ONLY its tokens)
  3. exact tick delivery (datetime fields preserved -> zero drift vs real KiteTicker)
  4. fault isolation (a consumer whose on_ticks raises never stalls the bus or peers)
  5. client auto-reconnect across a full bus restart

Run:  .venv\\Scripts\\python.exe live_trading_options/tickbus/tests/test_tickbus.py
"""

import sys
import time
import socket
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # .../live_trading_options

from tickbus.bus_server import TickBus
from tickbus.bus_client import BusTicker


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait(cond, timeout=3.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


class FakeTicker:
    """Stand-in for KiteTicker: records subscriptions and lets the test push ticks."""
    MODE_LTP = "ltp"
    MODE_QUOTE = "quote"
    MODE_FULL = "full"

    def __init__(self):
        self.on_ticks = self.on_connect = self.on_close = self.on_error = None
        self.subscribed = set()
        self.modes = {}

    def connect(self, threaded=True):
        if self.on_connect:
            self.on_connect(self, {})

    def subscribe(self, tokens):
        self.subscribed.update(int(t) for t in tokens)

    def set_mode(self, mode, tokens):
        for t in tokens:
            self.modes[int(t)] = mode

    def push(self, ticks):
        if self.on_ticks:
            self.on_ticks(self, ticks)


def test_fanout_union_and_datetime():
    ft = FakeTicker()
    bus = TickBus(ft, port=_free_port(), log=lambda *a: None)
    bus.start(threaded=True)
    try:
        a_rx, b_rx = [], []
        a = BusTicker(host=bus.host, port=bus.port, log=lambda *a: None)
        b = BusTicker(host=bus.host, port=bus.port, log=lambda *a: None)
        a.on_ticks = lambda ws, t: a_rx.extend(t)
        b.on_ticks = lambda ws, t: b_rx.extend(t)
        a.connect(threaded=True)
        b.connect(threaded=True)
        assert _wait(lambda: a._sock is not None and b._sock is not None), "clients did not connect"

        a.subscribe([1, 2]); a.set_mode(a.MODE_FULL, [1, 2])
        b.subscribe([2, 3]); b.set_mode(b.MODE_LTP, [2, 3])
        assert _wait(lambda: ft.subscribed == {1, 2, 3}), f"union wrong: {ft.subscribed}"
        # token 2 wanted FULL (by A) and LTP (by B) -> highest (FULL) must win
        assert _wait(lambda: ft.modes.get(2) == "full" and ft.modes.get(3) == "ltp"), f"modes wrong: {ft.modes}"

        ts = dt.datetime(2026, 10, 22, 9, 15, 0)
        ft.push([{"instrument_token": 1, "last_price": 10.0},
                 {"instrument_token": 2, "last_price": 20.0, "exchange_timestamp": ts},
                 {"instrument_token": 3, "last_price": 30.0}])
        assert _wait(lambda: len(a_rx) >= 2 and len(b_rx) >= 2), f"fan-out slow: A={a_rx} B={b_rx}"

        assert {t["instrument_token"] for t in a_rx} == {1, 2}, f"A got {a_rx}"
        assert {t["instrument_token"] for t in b_rx} == {2, 3}, f"B got {b_rx}"
        tok2_for_a = [t for t in a_rx if t["instrument_token"] == 2][0]
        assert isinstance(tok2_for_a["exchange_timestamp"], dt.datetime), "datetime not preserved across the bus"
        a.close(); b.close()
    finally:
        bus.stop()


def test_consumer_fault_isolation():
    ft = FakeTicker()
    bus = TickBus(ft, port=_free_port(), log=lambda *a: None)
    bus.start(threaded=True)
    try:
        good_rx = []
        bad = BusTicker(host=bus.host, port=bus.port, log=lambda *a: None)
        good = BusTicker(host=bus.host, port=bus.port, log=lambda *a: None)
        bad.on_ticks = lambda ws, t: (_ for _ in ()).throw(RuntimeError("consumer bug"))
        good.on_ticks = lambda ws, t: good_rx.extend(t)
        bad.connect(threaded=True); good.connect(threaded=True)
        assert _wait(lambda: bad._sock is not None and good._sock is not None)
        bad.subscribe([7]); good.subscribe([7])
        assert _wait(lambda: ft.subscribed == {7})

        ft.push([{"instrument_token": 7, "last_price": 1.0}])
        ft.push([{"instrument_token": 7, "last_price": 2.0}])
        # the throwing consumer must not stop the healthy one from receiving BOTH ticks
        assert _wait(lambda: len(good_rx) >= 2), f"healthy consumer starved by a buggy peer: {good_rx}"
        bad.close(); good.close()
    finally:
        bus.stop()


def test_client_reconnects_across_bus_restart():
    port = _free_port()
    ft1 = FakeTicker()
    bus1 = TickBus(ft1, port=port, log=lambda *a: None)
    bus1.start(threaded=True)
    rx = []
    c = BusTicker(host="127.0.0.1", port=port, log=lambda *a: None)
    c.on_ticks = lambda ws, t: rx.extend(t)
    c.connect(threaded=True)
    try:
        assert _wait(lambda: c._sock is not None)
        c.subscribe([9]); c.set_mode(c.MODE_LTP, [9])
        assert _wait(lambda: ft1.subscribed == {9})
        bus1.stop()                                   # kill the bus
        assert _wait(lambda: c._sock is None, timeout=5)   # client notices the drop

        ft2 = FakeTicker()                            # new bus on the SAME port
        bus2 = TickBus(ft2, port=port, log=lambda *a: None)
        bus2.start(threaded=True)
        try:
            # client must reconnect AND re-push its subscription without any re-subscribe call
            assert _wait(lambda: c._sock is not None and ft2.subscribed == {9}, timeout=10), \
                "client did not auto-reconnect + re-subscribe"
            ft2.push([{"instrument_token": 9, "last_price": 99.0}])
            assert _wait(lambda: any(t["last_price"] == 99.0 for t in rx), timeout=5), \
                "no ticks after reconnect"
        finally:
            bus2.stop()
    finally:
        c.close()


def _run_all():
    tests = [test_fanout_union_and_datetime, test_consumer_fault_isolation,
             test_client_reconnects_across_bus_restart]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
