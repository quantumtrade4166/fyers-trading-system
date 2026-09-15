"""
Tests for the September-entry fixes (2026-09-14). No broker, no network.

    python -m deployment.dualmom_live.tests.test_entry_rules
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

from deployment.dualmom_live import config as C
from deployment.dualmom_live import rebalance as R
import deployment.dualmom_live_api as API

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


print("\n=== 1. cash reserve: never size the last rupee ===")
names = [f"S{i:02d}" for i in range(40)]
sig = {"signal": "IN", "date": "2026-08-31", "top_n": 40,
       "holdings": [{"symbol": s, "weight": 1 / 40} for s in names]}
marks = {s: 97.3 + i for i, s in enumerate(names)}
p = R.plan(sig, {}, marks, 1_000_000)
spent = sum(o["qty"] * o["mark"] for o in p["buys"])
check(f"reserve is a fixed Rs 15,000 (got {C.CASH_RESERVE_RS:,})", C.CASH_RESERVE_RS == 15_000)
check(f"at least Rs 15,000 left in cash (left Rs {1_000_000 - spent:,.0f})",
      1_000_000 - spent >= 15_000, spent)
worst = sum(o["qty"] * o["mark"] * (1 + C.MARKETABLE_BUFFER) for o in p["buys"])
check(f"even at the 0.5% limit cap, total fits the cash (Rs {worst:,.0f})",
      worst < 1_000_000, worst)
check("still buys all 40 names", len(p["buys"]) == 40, len(p["buys"]))

print("\n=== 2. mid-month preview uses THIS month's signal date ===")
class FakeS:
    @staticmethod
    def load_prices():
        idx = pd.to_datetime(["2026-08-27", "2026-08-28", "2026-08-31",
                              "2026-09-01", "2026-09-10", "2026-09-11"])
        return pd.DataFrame({"X": range(len(idx))}, index=idx)

check("15-Sep -> 31-Aug (not 11-Sep's mid-month close)",
      API._month_signal_date(FakeS, date(2026, 9, 15)) == date(2026, 8, 31),
      API._month_signal_date(FakeS, date(2026, 9, 15)))
check("1-Sep -> 31-Aug", API._month_signal_date(FakeS, date(2026, 9, 1)) == date(2026, 8, 31))
check("a month with no prior data -> None",
      API._month_signal_date(FakeS, date(2026, 8, 20)) is None)

print("\n=== 3. the service shares the dashboard's Kotak session ===")
import inspect
import deployment.dualmom_service as SVC
src = inspect.getsource(SVC._client)
check("service _client() reuses dualmom_live_api._get_client", "_get_client" in src, src)
check("and does NOT log in on its own", "A.login(" not in src and "login(" not in src.replace("_get_client", ""), src)

print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"    FAILED: {f}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
