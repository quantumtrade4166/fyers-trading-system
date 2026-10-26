"""Warm-up history source: Kite first, Fyers only as a fallback (no network).

    .venv\\Scripts\\python.exe -m pytest live_trading_options/nifty_st_directional/test_engine_history.py -q
"""
import datetime as dt

import pandas as pd

import live_trading_options.nifty_st_directional.engine as LE

NOW = dt.datetime(2026, 10, 27, 9, 12, 0)


def rows(day, n=3):
    t0 = dt.datetime.combine(day, dt.time(9, 15))
    return [{"date": t0 + dt.timedelta(minutes=i), "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}
            for i in range(n)]


class FakeKite:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def historical_data(self, token, start, end, interval):
        self.calls += 1
        if self.fail:
            raise RuntimeError("kite down")
        return rows(dt.date(2026, 10, 26)) + rows(dt.date(2026, 10, 23))


class FakeFyers:
    def __init__(self):
        self.calls = 0

    def history(self, req):
        self.calls += 1
        t0 = dt.datetime(2026, 10, 26, 9, 15, tzinfo=LE.IST).timestamp()
        return {"candles": [[int(t0) + 60 * i, 1, 2, 0.5, 1.5, 10] for i in range(3)]}


def make(kite):
    eng = LE.Engine(clock=lambda: NOW)
    eng.kite = kite
    eng.fy = FakeFyers()
    eng.event = lambda *a, **k: None
    return eng


def test_kite_is_the_primary_source():
    eng = make(FakeKite())
    df = eng.history("NSE:NIFTY50-INDEX", 256265, 1)
    assert eng.kite.calls == 1 and eng.fy.calls == 0
    assert sorted(df["datetime"].dt.date.unique()) == [dt.date(2026, 10, 26)]      # 1 prior session
    assert len(df) == 3


def test_fyers_only_when_kite_fails():
    eng = make(FakeKite(fail=True))
    df = eng.history("NSE:NIFTY50-INDEX", 256265, 1)
    assert eng.kite.calls == 1 and eng.fy.calls == 1 and len(df) == 3
