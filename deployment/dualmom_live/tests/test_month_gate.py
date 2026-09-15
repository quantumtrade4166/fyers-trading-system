"""
Tests for the monthly rebalance gate. No broker, no network.

    python -m deployment.dualmom_live.tests.test_month_gate
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding="utf-8")

from deployment.dualmom_live import month_gate as G

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def sessions(start: date, end: date, holidays=()):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return set(out)


print("\n=== 1. THE BUG: live data always ends today ===")
# data through Fri 11-Sep (the refresh just appended it); Sept already rebalanced on 1-Sep
data = sessions(date(2026, 8, 1), date(2026, 9, 11))
d = G.decide(date(2026, 9, 11), data, "2026-09", market_open=True)
check("mid-month with the month done -> skip (old code fired here)", d["action"] == "skip", d)

print("\n=== 2. first session of a new month ===")
data = sessions(date(2026, 8, 1), date(2026, 9, 30))
d = G.decide(date(2026, 10, 1), data, "2026-09", market_open=True)
check("1-Oct runs", d["action"] == "run", d)
check("signal on the Sept month-end close (30-Sep)", d["signal_date"] == date(2026, 9, 30), d)

print("\n=== 3. month-end falls on a holiday — no calendar needed ===")
data = sessions(date(2026, 8, 1), date(2026, 9, 30), holidays={date(2026, 9, 30)})
d = G.decide(date(2026, 10, 1), data, "2026-09", market_open=True)
check("signal date is the real last session (29-Sep)", d["signal_date"] == date(2026, 9, 29), d)

print("\n=== 4. first day of the month is itself a holiday ===")
data = sessions(date(2026, 8, 1), date(2026, 9, 30))
d = G.decide(date(2026, 10, 2), data, "2026-09", market_open=False)
check("exchange closed -> retry, not run", d["action"] == "retry", d)
d = G.decide(date(2026, 10, 5), data, "2026-09", market_open=True)
check("next open session runs on the SAME month-end close",
      d["action"] == "run" and d["signal_date"] == date(2026, 9, 30), d)

print("\n=== 5. retry never trades twice ===")
check("after mark_done the month is skipped",
      G.decide(date(2026, 10, 6), data, "2026-10", market_open=True)["action"] == "skip")

print("\n=== 6. broken data refresh ===")
stale = sessions(date(2026, 8, 1), date(2026, 9, 18))     # refresh died on 18-Sep
d = G.decide(date(2026, 10, 1), stale, "2026-09", market_open=True)
check("stale month-end close -> refuse", d["action"] == "refuse", d)
check("the reason names the broken refresh", "refresh" in d["reason"], d)

print("\n=== 7. too late in the month ===")
d = G.decide(date(2026, 10, 14), data, "2026-09", market_open=True)
check("13 days in, never rebalanced -> refuse, needs a human", d["action"] == "refuse", d)
d = G.decide(date(2026, 9, 14), sessions(date(2026, 8, 1), date(2026, 9, 11)), None, True)
check("this month (built mid-Sept) is NOT auto-traded late", d["action"] == "refuse", d)

print("\n=== 8. no data at all ===")
check("empty data -> refuse", G.decide(date(2026, 10, 1), set(), None, True)["action"] == "refuse")

print("\n=== 9. heartbeat detects a paused VM ===")
import deployment.dualmom_service as SVC
logs = []
SVC._log = lambda m: logs.append(m)
import time as _t
real_time, real_mono = _t.time, _t.monotonic
clock = {"wall": 1_000_000.0, "mono": 500.0}
_t.time = lambda: clock["wall"]
_t.monotonic = lambda: clock["mono"]
SVC._hb.update(wall=None, mono=None)
SVC._job_heartbeat()                                   # baseline
clock["wall"] += 60; clock["mono"] += 60
SVC._job_heartbeat()
check("normal minute: no alarm", not logs, logs)
clock["wall"] += 8 * 3600; clock["mono"] += 60          # VM paused 8h
SVC._job_heartbeat()
check("8h pause flagged", any("VM PAUSE DETECTED" in m for m in logs), logs)
_t.time, _t.monotonic = real_time, real_mono


print("\n=== 10. the exchange-open probe, on the shapes seen live on 14-Sep ===")
import pytz as _pytz
_IST = _pytz.timezone("Asia/Kolkata")
def _epoch(d, hh=9, mm=15):
    return int(_IST.localize(__import__("datetime").datetime(d.year, d.month, d.day, hh, mm)).timestamp())
class FyTraded:      # shape of Fri 11-Sep: s=ok, candles from 09:15
    def history(self, req):
        d = date.fromisoformat(req["range_from"])
        return {"s": "ok", "candles": [[_epoch(d), 1, 1, 1, 1, 0], [_epoch(d, 9, 16), 1, 1, 1, 1, 0]]}
class FyHoliday:     # shape of Mon 14-Sep: s=no_data
    def history(self, req):
        return {"s": "no_data", "candles": []}
class FyBroken:
    def history(self, req):
        raise RuntimeError("token expired")
class FyStale:       # ok, but the candles are from ANOTHER day
    def history(self, req):
        return {"s": "ok", "candles": [[_epoch(date(2026, 9, 11)), 1, 1, 1, 1, 0]]}
check("trading day (candles today) -> True",
      G.market_traded_today(None, date(2026, 10, 1), FyTraded()) is True)
check("holiday (s=no_data) -> False",
      G.market_traded_today(None, date(2026, 10, 1), FyHoliday()) is False)
check("Fyers broken + no Kotak -> None (unknown), not False",
      G.market_traded_today(None, date(2026, 10, 1), FyBroken()) is None)
check("candles from a different day do NOT count as trading today",
      G.market_traded_today(None, date(2026, 10, 1), FyStale()) is False)

m = G._KOTAK_DATE.search("Fri Sep 11 2026 05:30:00 GMT+0530 (India Standard Time)")
check("Kotak date string parses", m and m.group(3) == "11" and m.group(4) == "2026", m)
check("Kotak bare number does NOT parse as a date", G._KOTAK_DATE.search("1789119910") is None)
d = G.decide(date(2026, 10, 1), sessions(date(2026, 8, 1), date(2026, 9, 30)), "2026-09", None)
check("unknown open-state -> retry, not run", d["action"] == "retry" and "could not" in d["reason"], d)
print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"    FAILED: {f}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
