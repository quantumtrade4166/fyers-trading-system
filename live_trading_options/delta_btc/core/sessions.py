"""
core/sessions.py — what "a trading day" means when the market never closes.
===========================================================================

The NSE strategy inherited its clock from the exchange: 09:15-15:30, one session,
no ambiguity. BTC trades 24/7, so the session is a CHOICE — and it is the choice
this whole package exists to test. Three profiles run side by side on one tick
stream, and after a month of paper the difference between their books is the
answer.

The one fixed point is settlement: every daily BTC option settles at 17:30 IST,
and the next day's chain lists at the same moment. Every profile is defined
against that.

    A  ist_day      09:30 -> 17:10 IST, same day       ~7.7h exposure
       Sells the contract expiring TODAY. You are awake for all of it.

    B  full_cycle   17:35 -> 17:10 IST next day       ~23.6h exposure
       Enters just after the new chain lists, holds the whole expiry cycle
       through US and Asian hours, exits 20 minutes before settlement.

    C  continuous   17:35 -> 17:10 IST next day       ~23.6h, never stops
       Same clock as B, different temperament: a stop-out or a max-loss does not
       end the cycle, it re-enters at the next adjustment window. B asks "what
       does one strangle per expiry earn"; C asks "what does always being short
       earn".

The 25-minute flat gap between 17:10 and 17:35 is not a design choice — the
contract settles at 17:30 and its replacement does not exist until then. Even
"continuous" is flat across it.

DELIBERATELY NOT VARIED IN v1: the stop-tightening schedule. The NSE strategy
tightens its stop through expiry day (40->30->20). Giving that to A but not to
B/C — or picking different schedules per profile — would confound the experiment:
a difference in the results could then come from the schedule rather than from
the session length, which is the single variable this month is meant to measure.
All three run a FIXED stop at 2x the entry target. Schedules are a later study.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as dt


def _t(s: str) -> dt.time:
    return dt.datetime.strptime(s, "%H:%M").time()


class SessionProfile:
    """One profile's clock and temperament. Pure time arithmetic — no I/O and no
    market state — so every boundary is exactly testable."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.label = cfg.get("label", name)
        self.entry_time = _t(cfg["entry_time"])
        self.square_off = _t(cfg["square_off"])
        self.every = int(cfg.get("adjust_every_minutes", 15))
        self.window_seconds = int(cfg.get("adjust_window_seconds", 60))
        # Windows start this long after entry and stop this long before the exit,
        # so an adjustment can never fire on top of the entry itself, nor moments
        # before the square-off would undo it anyway.
        self.first_offset = int(cfg.get("first_adjust_after_minutes", 15))
        self.last_offset = int(cfg.get("last_adjust_before_minutes", 10))

        # temperament
        self.ends_on_both_stopped = bool(cfg.get("ends_on_both_stopped", True))
        self.ends_on_max_loss = bool(cfg.get("ends_on_max_loss", True))
        self.max_fresh_entries = int(cfg.get("max_fresh_entries", 3))

        # strategy parameters for this profile
        self.target = float(cfg["target_premium"])
        self.sl = float(cfg["sl_premium"])
        self.prefer_min = cfg.get("prefer_min")
        self.prefer_max = cfg.get("prefer_max")
        self.contracts = int(cfg.get("contracts", 100))

    # ── shape ────────────────────────────────────────────────────────────
    @property
    def spans_midnight(self) -> bool:
        return self.square_off <= self.entry_time

    @property
    def exposure_hours(self) -> float:
        a = self.entry_time.hour * 60 + self.entry_time.minute
        b = self.square_off.hour * 60 + self.square_off.minute
        return round(((b - a) % (24 * 60)) / 60.0, 2)

    # ── which cycle are we in ────────────────────────────────────────────
    def cycle_bounds(self, now: dt.datetime):
        """(start, end) of the cycle `now` falls inside, or None in the flat gap
        between cycles.

        The gap is real and matters: for A it is the 16 hours between one session
        and the next; for B and C it is the 25 minutes across settlement.
        """
        d = now.date()
        if not self.spans_midnight:
            start = dt.datetime.combine(d, self.entry_time)
            end = dt.datetime.combine(d, self.square_off)
            return (start, end) if start <= now < end else None
        # spans midnight: either today's cycle has begun, or yesterday's is still
        # running into this morning
        if now.time() >= self.entry_time:
            start = dt.datetime.combine(d, self.entry_time)
            return start, dt.datetime.combine(d + dt.timedelta(days=1), self.square_off)
        if now.time() < self.square_off:
            start = dt.datetime.combine(d - dt.timedelta(days=1), self.entry_time)
            return start, dt.datetime.combine(d, self.square_off)
        return None

    def cycle_key(self, now: dt.datetime):
        """Stable id for the cycle — what the controller latches its entry and its
        counters against, so one cycle enters exactly once however many ticks
        arrive. Keyed on the cycle's START datetime, which stays unique even when
        a cycle spans two dates."""
        b = self.cycle_bounds(now)
        return None if b is None else f"{self.name}@{b[0]:%Y-%m-%dT%H:%M}"

    def in_session(self, now: dt.datetime) -> bool:
        return self.cycle_bounds(now) is not None

    # ── entry ────────────────────────────────────────────────────────────
    def is_entry_time(self, now: dt.datetime, grace_seconds: int = 120) -> bool:
        """True from the entry moment until `grace_seconds` after it.

        The grace exists because the engine is tick-driven: at exactly 17:35:00.000
        there may be no tick in flight, and a strict equality test would miss the
        entry for the whole cycle. The controller latches, so this being true for
        two minutes still yields exactly one entry.
        """
        b = self.cycle_bounds(now)
        if b is None:
            return False
        return b[0] <= now < b[0] + dt.timedelta(seconds=grace_seconds)

    # ── adjustment windows ───────────────────────────────────────────────
    def window_starts(self, now: dt.datetime) -> list:
        """Every window start in the CURRENT cycle, as full datetimes — full
        datetimes rather than times because a cycle can span two dates, and
        "18:00" alone would be ambiguous about which day's 18:00 it meant.

        Windows are SNAPPED TO THE WALL CLOCK (:00/:15/:30/:45 at the default
        cadence) rather than counted from each profile's own entry time. Two
        reasons, and the second is the important one:

          - it is how the NSE strategy reads, and how a human reads a chart
          - all three profiles then evaluate at the SAME instants. Counting from
            entry would put A on the quarters and B/C five minutes off them, so
            the two books would be deciding on different snapshots of the same
            market — a difference in the month's results could then come from
            the offset rather than from the session, which is exactly the
            confound this experiment is built to avoid.
        """
        b = self.cycle_bounds(now)
        if b is None:
            return []
        start, end = b
        first = start + dt.timedelta(minutes=self.first_offset)
        # snap UP to the next wall-clock multiple of `every`; never earlier than
        # first_offset asked for, so the entry still gets its settling period
        mins = first.hour * 60 + first.minute
        rem = mins % self.every
        if rem or first.second or first.microsecond:
            first = (first.replace(second=0, microsecond=0)
                     + dt.timedelta(minutes=self.every - rem))
        last = end - dt.timedelta(minutes=self.last_offset)
        out, cur = [], first
        while cur <= last:
            out.append(cur)
            cur += dt.timedelta(minutes=self.every)
        return out

    def window_key(self, now: dt.datetime):
        """The key of the window `now` is inside, else None. A window is the
        half-open interval [start, start + window_seconds)."""
        for w in self.window_starts(now):
            if w <= now < w + dt.timedelta(seconds=self.window_seconds):
                return f"{w:%Y-%m-%dT%H:%M}"
        return None

    def next_window(self, now: dt.datetime):
        for w in self.window_starts(now):
            if w > now:
                return w
        return None

    def seconds_to_next_window(self, now: dt.datetime):
        nxt = self.next_window(now)
        return None if nxt is None else int((nxt - now).total_seconds())

    # ── exit ─────────────────────────────────────────────────────────────
    def past_square_off(self, now: dt.datetime) -> bool:
        """True once the current cycle's exit time has passed.

        Outside a cycle this is True as well — a controller waking in the gap
        holding anything must flatten it, not wait for a window that will never
        come.
        """
        b = self.cycle_bounds(now)
        return True if b is None else now >= b[1]

    # ── economics ────────────────────────────────────────────────────────
    def worst_case_both_stopped(self, contract_value: float = 0.001) -> float:
        """USD lost if BOTH legs enter at target and stop out.

        The max-loss limit MUST sit above this or it fires first and the per-leg
        stops never get to work — the trap documented on the NSE side, put here in
        the one place every profile is forced to look at it.
        """
        return round((self.sl - self.target) * contract_value * self.contracts * 2, 2)

    def credit_at_target(self, contract_value: float = 0.001) -> float:
        """USD collected if both legs fill exactly at the target premium."""
        return round(self.target * contract_value * self.contracts * 2, 2)

    def describe(self) -> str:
        temperament = "ends" if self.ends_on_both_stopped else "continues"
        return (f"{self.name:<11} {self.entry_time:%H:%M}->{self.square_off:%H:%M} IST "
                f"({self.exposure_hours:>5.2f}h)  target ${self.target:g} sl ${self.sl:g} "
                f"x{self.contracts}  credit ${self.credit_at_target():g} "
                f"worst ${self.worst_case_both_stopped():g}  "
                f"{temperament}-on-stopout")


def load_profiles(params: dict) -> dict:
    """Build every ENABLED profile from config."""
    out = {}
    for name, cfg in (params.get("sessions") or {}).items():
        if cfg.get("enabled", True):
            out[name] = SessionProfile(name, cfg)
    return out
