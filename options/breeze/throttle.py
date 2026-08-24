"""Rate limiting for Breeze.

Two limits, enforced differently:

  100 calls/minute — a rolling in-process window; we just sleep when full.
  5000 calls/day   — a hard daily budget that must survive process restarts,
                     so the counter is persisted to disk and keyed by date.

The daily cap is the real constraint on any bulk download, so `remaining()` is
what the downloader checks before starting each contract.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import threading
import time
from collections import deque
from datetime import date
from pathlib import Path

from options.breeze.config import (
    MAX_CALLS_PER_DAY,
    MAX_CALLS_PER_MINUTE,
    PROJECT_ROOT,
)

BUDGET_PATH = PROJECT_ROOT / "config" / "breeze_call_budget.json"


class DailyBudgetExhausted(RuntimeError):
    """Raised when the 5000 calls/day ceiling is hit."""


class Throttle:
    def __init__(self, safety_margin: int = 50, verbose: bool = True,
                 ignore_daily_cap: bool = False,
                 per_minute: int = MAX_CALLS_PER_MINUTE):
        # Leave headroom so an interactive probe/session call never gets locked out
        # by a bulk download that ran the budget to exactly zero.
        self.daily_cap = MAX_CALLS_PER_DAY - safety_margin
        self.per_minute = per_minute
        # When ignoring the cap we keep counting (so we know where the server
        # actually stops us) but never self-block — the server becomes the limit.
        self.ignore_daily_cap = ignore_daily_cap
        self.verbose = verbose
        self._minute_window: deque[float] = deque()
        # Downloads run concurrently, so the counters need guarding. The lock is
        # never held across a network call — only around bookkeeping.
        self._lock = threading.Lock()
        self._load_budget()

    def _load_budget(self) -> None:
        today = date.today().isoformat()
        self.day = today
        self.used = 0
        if BUDGET_PATH.exists():
            try:
                data = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
                if data.get("date") == today:
                    self.used = int(data.get("used", 0))
            except (json.JSONDecodeError, ValueError):
                pass  # corrupt budget file — start today's count from zero

    def _save_budget(self) -> None:
        BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
        BUDGET_PATH.write_text(
            json.dumps({"date": self.day, "used": self.used}), encoding="utf-8"
        )

    def remaining(self) -> int:
        # Roll over if the process has been running across midnight.
        if self.day != date.today().isoformat():
            self._load_budget()
        return max(0, self.daily_cap - self.used)

    def acquire(self) -> None:
        """Block until one call may be made. Raises if the daily budget is gone.

        Safe to call from multiple worker threads. Any sleeping happens OUTSIDE
        the lock so a waiting thread never blocks the others' bookkeeping.
        """
        while True:
            with self._lock:
                if self.remaining() <= 0 and not self.ignore_daily_cap:
                    raise DailyBudgetExhausted(
                        f"Daily Breeze budget spent ({self.used}/{self.daily_cap} "
                        f"calls on {self.day}). Resume tomorrow — the manifest "
                        "will pick up where it stopped."
                    )

                now = time.monotonic()
                while self._minute_window and now - self._minute_window[0] >= 60.0:
                    self._minute_window.popleft()

                if len(self._minute_window) < self.per_minute:
                    self._minute_window.append(now)
                    self.used += 1
                    self._save_budget()
                    return

                sleep_for = 60.0 - (now - self._minute_window[0]) + 0.05

            if self.verbose:
                print(f"    [throttle] minute cap reached, sleeping {sleep_for:.1f}s")
            time.sleep(max(0.05, sleep_for))
