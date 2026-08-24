"""
core/windows.py — adjustment-window timing.
===========================================

The strategy may adjust ONLY inside a 1-minute window every 15 minutes:

    09:45:00-09:45:59, 10:00:00-10:00:59, 10:15:00-10:15:59, ... 15:00:00-15:00:59

Outside those 60-second slots nothing is ever closed or re-entered. That is the
whole point of the rule — it stops the strategy reacting to every wiggle — so the
timing lives in its own module with no I/O, making it directly testable
(`test_windows.py`) instead of being buried in the controller's tick handler.

Two separate ideas, deliberately not conflated:
  window_key(now)  -> WHICH window we are inside right now (None outside one)
  ...the controller then remembers the keys it has already acted on, so a window
  fires exactly ONCE even though ticks arrive ~every 200ms for its full 60s.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as dt


def _t(s: str) -> dt.time:
    return dt.datetime.strptime(s, "%H:%M").time()


def window_starts(first: str = "09:45", last: str = "15:00",
                  every_minutes: int = 15) -> list[dt.time]:
    """Every window's START time, inclusive of both `first` and `last`."""
    f, l = _t(first), _t(last)
    out, cur = [], dt.datetime(2000, 1, 1, f.hour, f.minute)
    end = dt.datetime(2000, 1, 1, l.hour, l.minute)
    while cur <= end:
        out.append(cur.time())
        cur += dt.timedelta(minutes=every_minutes)
    return out


def window_key(now: dt.datetime, *, first: str = "09:45", last: str = "15:00",
               every_minutes: int = 15, window_seconds: int = 60) -> str | None:
    """"HH:MM" of the window `now` falls inside, else None.

    A window is the half-open interval [start, start + window_seconds). At
    exactly 09:46:00 we are OUT — the window was 09:45:00-09:45:59.999. Seconds
    matter here, which is why this takes a full datetime and not an "HH:MM"
    string like the rest of the engine's time handling.
    """
    for w in window_starts(first, last, every_minutes):
        start = now.replace(hour=w.hour, minute=w.minute, second=0, microsecond=0)
        if start <= now < start + dt.timedelta(seconds=window_seconds):
            return w.strftime("%H:%M")
    return None


def next_window(now: dt.datetime, *, first: str = "09:45", last: str = "15:00",
                every_minutes: int = 15) -> dt.datetime | None:
    """The next window START strictly after `now` (for the UI countdown), or None
    once the last window of the day has passed."""
    for w in window_starts(first, last, every_minutes):
        start = now.replace(hour=w.hour, minute=w.minute, second=0, microsecond=0)
        if start > now:
            return start
    return None


def seconds_to_next_window(now: dt.datetime, **kw) -> int | None:
    nxt = next_window(now, **kw)
    return None if nxt is None else int((nxt - now).total_seconds())


def is_entry_time(now: dt.datetime, entry_time: str = "09:30",
                  grace_seconds: int = 90) -> bool:
    """True from the entry time until `grace_seconds` after it.

    The grace period exists because the engine is tick-driven: at 09:30:00.000
    there may be no tick in flight, and a strict equality test would miss the
    entry entirely. It also covers a slightly late engine start. Entry is still
    fired only ONCE — the controller latches it.
    """
    e = _t(entry_time)
    start = now.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
    return start <= now < start + dt.timedelta(seconds=grace_seconds)


def past_square_off(now: dt.datetime, square_off: str = "15:14") -> bool:
    s = _t(square_off)
    return now.time() >= s
