"""
core/test_windows.py — adjustment-window timing tests.
======================================================

The window rule is the one piece of this strategy that cannot be verified by
watching it live (a bug fires an adjustment a minute early and you only find out
from the audit log the next day), so it is pinned down here.

Run:  python live_trading_options/delta_neutral/core/test_windows.py
"""

import sys
import datetime as dt
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.windows import (window_starts, window_key, next_window,
                          seconds_to_next_window, is_entry_time, past_square_off)

D = dt.date(2026, 8, 25)


def at(hms: str) -> dt.datetime:
    h, m, s = (list(map(int, hms.split(":"))) + [0])[:3]
    return dt.datetime.combine(D, dt.time(h, m, s))


PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


# ── the schedule itself ───────────────────────────────────────────────────
ws = window_starts()
check("first window is 09:45", ws[0].strftime("%H:%M"), "09:45")
check("last window is 15:00", ws[-1].strftime("%H:%M"), "15:00")
check("22 windows in the day (09:45→15:00)", len(ws), 22)
check("no 09:30 window (that's entry)", dt.time(9, 30) in ws, False)
check("10:00 is a window", dt.time(10, 0) in ws, True)
check("10:05 is NOT a window", dt.time(10, 5) in ws, False)

# ── the exact 1-minute boundary — the rule that must not slip ─────────────
check("09:44:59 → outside (too early)", window_key(at("09:44:59")), None)
check("09:45:00 → inside (first instant)", window_key(at("09:45:00")), "09:45")
check("09:45:30 → inside (mid-window)", window_key(at("09:45:30")), "09:45")
check("09:45:59 → inside (last instant)", window_key(at("09:45:59")), "09:45")
check("09:46:00 → outside (window closed)", window_key(at("09:46:00")), None)
check("09:46:01 → outside", window_key(at("09:46:01")), None)
check("09:50:00 → outside (between windows)", window_key(at("09:50:00")), None)

check("10:00:00 → inside", window_key(at("10:00:00")), "10:00")
check("10:15:45 → inside", window_key(at("10:15:45")), "10:15")
check("15:00:59 → inside (final window)", window_key(at("15:00:59")), "15:00")
check("15:01:00 → outside (day's windows done)", window_key(at("15:01:00")), None)
check("15:15:00 → outside (past all windows)", window_key(at("15:15:00")), None)
check("09:30:00 entry time is NOT a window", window_key(at("09:30:00")), None)

# ── countdown to the next window ──────────────────────────────────────────
check("next window after 09:31", next_window(at("09:31:00")).strftime("%H:%M"), "09:45")
check("next window after 09:45:30 is 10:00",
      next_window(at("09:45:30")).strftime("%H:%M"), "10:00")
check("seconds from 09:44:00 to 09:45", seconds_to_next_window(at("09:44:00")), 60)
check("seconds from 09:44:30 to 09:45", seconds_to_next_window(at("09:44:30")), 30)
check("no next window after 15:00", next_window(at("15:05:00")), None)

# ── entry + square-off gates ──────────────────────────────────────────────
check("09:29:59 not entry time", is_entry_time(at("09:29:59")), False)
check("09:30:00 IS entry time", is_entry_time(at("09:30:00")), True)
check("09:31:00 still inside entry grace", is_entry_time(at("09:31:00")), True)
check("09:32:00 past entry grace", is_entry_time(at("09:32:00")), False)
check("15:13:59 not square-off yet", past_square_off(at("15:13:59")), False)
check("15:14:00 IS square-off", past_square_off(at("15:14:00")), True)
check("15:20:00 past square-off", past_square_off(at("15:20:00")), True)

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
