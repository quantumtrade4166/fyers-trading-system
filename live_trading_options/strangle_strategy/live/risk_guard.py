"""
live/risk_guard.py — the deterministic safety gate.
===================================================

Separate from the strategy on purpose: even if the strategy logic has a bug and
"wants" to do something wrong (fire too many lots, exit a position it doesn't hold,
trade after a kill), every order passes through here first and the guard can refuse.
Strategy = driver, guard = brakes + airbags.

This is plain, tested, instant rules — never an LLM. It wraps the own-book Ledger.

Nothing here places orders; it returns allow/deny + instructions. The executor
places; the guard decides.
"""

import datetime as dt


def _t(s: str) -> dt.time:
    return dt.datetime.strptime(s, "%H:%M").time()


class RiskGuard:
    def __init__(self, ledger, *, max_cycles: int, mtm_stop: float,
                 entry_cutoff: str = "14:30", square_off: str = "15:15"):
        self.L = ledger
        self.max_cycles = max_cycles
        self.mtm_stop = abs(mtm_stop)
        self.entry_cutoff = _t(entry_cutoff)
        self.square_off = _t(square_off)
        self.killed = False
        self.kill_reason = None
        self._fired = set()          # trigger ids already acted on (duplicate guard)

    # ── the kill switch ───────────────────────────────────────────────────
    def kill(self, reason: str):
        """Stop all NEW orders immediately and record why. Open positions are then
        flattened by the caller (kill implies square-off)."""
        if not self.killed:
            self.killed = True
            self.kill_reason = reason

    # ── entry gate ────────────────────────────────────────────────────────
    def validate_entry(self, cycle_no: int, trigger_id: str, now: dt.datetime) -> tuple:
        """(allowed, reason). Blocks a NEW short on any guard violation."""
        if self.killed:
            return False, f"killed ({self.kill_reason})"
        if cycle_no > self.max_cycles:
            return False, f"max {self.max_cycles} cycles reached"
        if now.time() > self.entry_cutoff:
            return False, f"past entry cutoff {self.entry_cutoff:%H:%M}"
        if now.time() >= self.square_off:
            return False, "at/after square-off"
        if trigger_id in self._fired:
            return False, "duplicate trigger (already acted)"
        return True, "ok"

    def mark_fired(self, trigger_id: str):
        """Call once an entry for this trigger has actually been sent — so the same
        tick/trigger can never double-fire an order."""
        self._fired.add(trigger_id)

    # ── exit gate ─────────────────────────────────────────────────────────
    def validate_exit(self, now: dt.datetime) -> tuple:
        """Exits (closing risk) are ALWAYS allowed — a kill or cutoff must never
        trap an open position. The only 'no' is if there's nothing open."""
        if not self.L.open_shorts():
            return False, "nothing open to exit"
        return True, "ok"

    # ── continuous monitors (called every tick / cycle) ───────────────────
    def check_mtm(self, marks: dict) -> tuple:
        """(breached, mtm). If loss hits the stop -> kill + caller flattens + disables."""
        mtm = self.L.mtm(marks)
        if mtm <= -self.mtm_stop:
            self.kill(f"MTM stop hit ({mtm} <= -{self.mtm_stop})")
            return True, mtm
        return False, mtm

    def check_naked(self, ce_sym: str, pe_sym: str):
        """Return (symbol, units) of an uncovered leg to buy back NOW, or None."""
        return self.L.naked_leg(ce_sym, pe_sym)

    def check_reconcile(self) -> dict:
        """Book-vs-broker health. A rejected leg or a still-pending order after fills
        means don't assume state -> caller should halt + alert."""
        rejected = [o.order_id for o in self.L.rejected_orders()]
        pending = [o.order_id for o in self.L.pending_orders()]
        ok = not rejected and not pending
        return {"ok": ok, "rejected": rejected, "pending": pending}

    def must_square_off(self, now: dt.datetime) -> bool:
        return now.time() >= self.square_off
