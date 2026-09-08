"""
core/test_sessions.py — session-boundary tests for the three profiles.
======================================================================

Session timing is the single variable this month-long experiment measures, so
every boundary is pinned here rather than discovered live at 03:00 with a real
position on. The cases that matter are the ones a same-day session can never
express:

  - a cycle that SPANS MIDNIGHT (B and C run 17:35 -> 17:10 the next day)
  - the FLAT GAP between cycles, where `past_square_off` must be True so a
    controller waking up in it flattens instead of waiting for a window that is
    never coming
  - window keys that stay unique across two dates

Run:  .venv/Scripts/python.exe live_trading_options/delta_btc/core/test_sessions.py
"""

import sys
import json
import datetime as dt
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.sessions import SessionProfile, load_profiles

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def T(s: str) -> dt.datetime:
    """'2026-09-04 17:35' -> datetime, for readable test cases."""
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M")


PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text(encoding="utf-8"))
PROFILES = load_profiles(PARAMS)

check("all three profiles load", sorted(PROFILES), ["continuous", "full_cycle", "ist_day"])

A = PROFILES["ist_day"]
B = PROFILES["full_cycle"]
C = PROFILES["continuous"]

# ── shape ─────────────────────────────────────────────────────────────────
check("A does not span midnight", A.spans_midnight, False)
check("B spans midnight", B.spans_midnight, True)
check("A exposure is 7.67h", A.exposure_hours, 7.67)
check("B exposure is 23.58h", B.exposure_hours, 23.58)
check("C has the same clock as B", (C.entry_time, C.square_off), (B.entry_time, B.square_off))
check("C differs from B only in temperament",
      (C.ends_on_both_stopped, B.ends_on_both_stopped), (False, True))

# ── A: a same-day session ─────────────────────────────────────────────────
check("A in session at 09:30", A.in_session(T("2026-09-04 09:30")), True)
check("A in session at 12:00", A.in_session(T("2026-09-04 12:00")), True)
check("A NOT in session at 09:29", A.in_session(T("2026-09-04 09:29")), False)
check("A NOT in session at 17:10 (end is exclusive)",
      A.in_session(T("2026-09-04 17:10")), False)
check("A NOT in session overnight", A.in_session(T("2026-09-04 23:00")), False)
check("A past square-off at 17:10", A.past_square_off(T("2026-09-04 17:10")), True)
check("A past square-off in the overnight gap (flatten, do not wait)",
      A.past_square_off(T("2026-09-05 02:00")), True)
check("A not past square-off mid-session", A.past_square_off(T("2026-09-04 12:00")), False)

# ── B/C: a cycle that spans midnight ──────────────────────────────────────
check("B in session at 17:35", B.in_session(T("2026-09-04 17:35")), True)
check("B in session at 23:59", B.in_session(T("2026-09-04 23:59")), True)
check("B still in session at 02:00 the NEXT day", B.in_session(T("2026-09-05 02:00")), True)
check("B in session at 17:09 next day", B.in_session(T("2026-09-05 17:09")), True)
check("B NOT in session at 17:10 next day", B.in_session(T("2026-09-05 17:10")), False)
check("B NOT in session in the settlement gap (17:10-17:35)",
      B.in_session(T("2026-09-04 17:20")), False)
check("B past square-off inside the settlement gap",
      B.past_square_off(T("2026-09-04 17:20")), True)

# the cycle that 02:00 belongs to STARTED the previous evening
bounds = B.cycle_bounds(T("2026-09-05 02:00"))
check("B cycle at 02:00 started 17:35 the day before", bounds[0], T("2026-09-04 17:35"))
check("B cycle at 02:00 ends 17:10 the same morning", bounds[1], T("2026-09-05 17:10"))

# ── cycle keys latch one entry per cycle ──────────────────────────────────
check("B cycle key is stable across midnight",
      B.cycle_key(T("2026-09-04 18:00")) == B.cycle_key(T("2026-09-05 09:00")), True)
check("B cycle key changes on the next cycle",
      B.cycle_key(T("2026-09-05 18:00")) != B.cycle_key(T("2026-09-04 18:00")), True)
check("B cycle key names the profile and its start",
      B.cycle_key(T("2026-09-04 18:00")), "full_cycle@2026-09-04T17:35")
check("cycle key is None in the gap", B.cycle_key(T("2026-09-04 17:20")), None)
check("A and B never share a cycle key",
      A.cycle_key(T("2026-09-04 12:00")) != B.cycle_key(T("2026-09-04 12:00")), True)

# ── entry latching ────────────────────────────────────────────────────────
check("A entry fires at 09:30", A.is_entry_time(T("2026-09-04 09:30")), True)
check("A entry still true inside the grace", A.is_entry_time(T("2026-09-04 09:31")), True)
check("A entry false at 09:33 (grace is 120s)",
      A.is_entry_time(T("2026-09-04 09:33")), False)
check("A entry false at 12:00", A.is_entry_time(T("2026-09-04 12:00")), False)
check("B entry fires at 17:35", B.is_entry_time(T("2026-09-04 17:35")), True)
check("B entry does NOT fire the next morning", B.is_entry_time(T("2026-09-05 09:00")), False)

# ── adjustment windows ────────────────────────────────────────────────────
aw = A.window_starts(T("2026-09-04 12:00"))
check("A first window is 15 min after entry", aw[0], T("2026-09-04 09:45"))
check("A last window is 10 min before square-off", aw[-1], T("2026-09-04 17:00"))
check("A windows are 15 min apart", (aw[1] - aw[0]).total_seconds(), 900.0)
check("A has 30 windows", len(aw), 30)
check("A windows sit on clock quarters", {w.minute for w in aw}, {0, 15, 30, 45})
check("B windows sit on the SAME clock quarters as A",
      {w.minute for w in B.window_starts(T("2026-09-04 20:00"))}, {0, 15, 30, 45})

check("A window key inside the minute",
      A.window_key(T("2026-09-04 09:45")), "2026-09-04T09:45")
check("A window key is None one minute later",
      A.window_key(T("2026-09-04 09:46")), None)
check("A window key is None between windows",
      A.window_key(T("2026-09-04 09:50")), None)
# seconds matter: the window is [start, start+60)
check("window is open at :00:59",
      A.window_key(T("2026-09-04 09:45") + dt.timedelta(seconds=59)), "2026-09-04T09:45")
check("window is shut at :01:00",
      A.window_key(T("2026-09-04 09:45") + dt.timedelta(seconds=60)), None)

bw = B.window_starts(T("2026-09-04 20:00"))
check("B first window snaps up to the clock quarter 18:00",
      bw[0], T("2026-09-04 18:00"))
check("B last window is 17:00 the next day", bw[-1], T("2026-09-05 17:00"))
check("B windows cross midnight", any(w.date() > dt.date(2026, 9, 4) for w in bw), True)
check("B has 93 windows over the cycle", len(bw), 93)
# the key must disambiguate the two 09:45s a spanning cycle could contain
check("B window keys carry the date",
      B.window_key(T("2026-09-05 09:45")), "2026-09-05T09:45")
check("B window keys are all unique", len(set(f"{w:%Y-%m-%dT%H:%M}" for w in bw)), len(bw))
check("no windows outside a cycle", B.window_starts(T("2026-09-04 17:20")), [])

# ── countdown ─────────────────────────────────────────────────────────────
check("next window from 09:46 is 10:00", A.next_window(T("2026-09-04 09:46")),
      T("2026-09-04 10:00"))
check("seconds to next window", A.seconds_to_next_window(T("2026-09-04 09:50")), 600)
check("no next window after the last one", A.next_window(T("2026-09-04 17:05")), None)

# ── economics: the max-loss trap ──────────────────────────────────────────
check("A worst case both stopped is $14", A.worst_case_both_stopped(), 14.0)
check("B worst case both stopped is $30", B.worst_case_both_stopped(), 30.0)
check("A credit at target is $14", A.credit_at_target(), 14.0)
check("B credit at target is $30", B.credit_at_target(), 30.0)
for name, p in PROFILES.items():
    limit = PARAMS["max_loss_usd"][name]
    check(f"{name}: max loss ${limit} sits above the worst case "
          f"${p.worst_case_both_stopped()}", limit > p.worst_case_both_stopped(), True)

# ── a profile with a broken config must be catchable, not silent ──────────
bad = SessionProfile("bad", {"entry_time": "09:30", "square_off": "17:10",
                             "target_premium": 100, "sl_premium": 120,
                             "contracts": 100})
check("a tight stop shrinks the worst case as expected",
      bad.worst_case_both_stopped(), 4.0)

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
