"""
cepe_imbalance/test_feed_watch.py - the feed silence watchdog (25-Nov-2026 live incident: Kotak feeds stayed
"connected" but sent no price for 7 minutes while a live book was in trade).

    .venv\Scripts\python.exe -m pytest live_trading_options/cepe_imbalance/test_feed_watch.py -q
"""
import datetime as dt

from live_trading_options.cepe_imbalance.feed import IST, MultiFeed


class _WS:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def feed(keys=("bse_fo|1", "bse_cm|SENSEX")):
    f = MultiFeed(sessions=None, on_ticks=lambda b: None)
    f.want(list(keys))
    f.ws = _WS()
    f.authed = True
    return f


WED_1000 = dt.datetime(2026, 11, 25, 10, 0, tzinfo=IST)
T = WED_1000.timestamp()


def test_silent_feed_is_reconnected():
    f = feed()
    f.last_tick_at = T - 25
    assert f.check_silence(T, WED_1000)
    assert f.ws.closed == 1 and "silent" in f.last_error


def test_fresh_ticks_or_recent_auth_do_nothing():
    f = feed()
    f.last_tick_at = T - 5
    assert not f.check_silence(T, WED_1000)
    f.last_tick_at = T - 100
    f._authed_at = T - 10                     # just re-authed: give it SILENCE_S to send prices
    assert not f.check_silence(T, WED_1000)


def test_closed_market_and_weekend_do_nothing():
    f = feed()
    f.last_tick_at = T - 3600
    assert not f.check_silence(T, dt.datetime(2026, 11, 25, 16, 0, tzinfo=IST))      # NSE/BSE closed
    assert not f.check_silence(T, dt.datetime(2026, 11, 28, 10, 0, tzinfo=IST))      # Saturday
    m = feed(("mcx_fo|5",))
    m.last_tick_at = T - 3600
    assert m.check_silence(T, dt.datetime(2026, 11, 25, 20, 0, tzinfo=IST))          # MCX evening: yes


def test_holiday_backs_off():
    f = feed()
    f.last_tick_at = T - 3600
    n = 0
    for i in range(10):                       # every 30 s for 5 minutes, never a tick (holiday)
        f._authed_at = 0
        n += f.check_silence(T + i * 30, WED_1000)
    assert n == MultiFeed.SILENT_TRIES        # then only every SILENT_BACKOFF_S
    assert f.check_silence(T + 9 * 30 + MultiFeed.SILENT_BACKOFF_S, WED_1000)


def test_a_real_tick_resets_the_backoff():
    f = feed()
    f.last_tick_at = T - 3600
    for i in range(3):
        f.check_silence(T + i * 30, WED_1000)
    f._silent_tries = 0                       # what the worker does on every real batch
    f.last_tick_at = T + 100
    assert f.check_silence(T + 125, WED_1000)
