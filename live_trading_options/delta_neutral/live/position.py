"""
live/position.py — the leg model for a strangle whose strikes move during the day.
=================================================================================

The VWAP strangle holds ONE pair of strikes all day, so its own-book Ledger
(orders keyed by symbol) is the whole story. This strategy replaces a leg every
time the 2x rule fires or a stop is hit, so on top of the ledger it needs to know
"which strike am I short on each side RIGHT NOW, and is that leg protected".

That is what `Leg` and `Position` are. They hold intent + protection state; the
Ledger underneath still owns fills and P&L, and remains the only thing that
decides how much to buy back.

Leg lifecycle:
    PENDING  -> placed, not yet filled
    OPEN     -> filled AND its stop is confirmed resting at the exchange
    NAKED    -> filled but the stop is NOT confirmed. This state is never allowed
                to persist: the controller either places the stop or buys the leg
                back. It exists so the condition is nameable and visible in the UI
                instead of being an invisible gap between two lines of code.
    STOPPED  -> the exchange stop fired and covered the leg
    CLOSED   -> deliberately bought back (adjustment or 15:14 square-off)
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as dt

PENDING, OPEN, NAKED, STOPPED, CLOSED = "PENDING", "OPEN", "NAKED", "STOPPED", "CLOSED"
CE, PE = "CE", "PE"


class Leg:
    def __init__(self, opt_type: str, strike: int, tradingsymbol: str, qty: int,
                 *, exchange: str = None, otm_level: int = None, reason: str = None):
        self.opt_type = opt_type
        self.strike = strike
        # The Kite tradingsymbol is the leg's identity AND its order key — one
        # field, because two that must be kept in sync is a bug waiting to happen.
        self.tradingsymbol = tradingsymbol
        self.exchange = exchange
        self.qty = qty
        self.otm_level = otm_level
        self.reason = reason                # why this strike was picked (for the log)

        self.status = PENDING
        self.entry_order_id = None
        self.entry_price = None
        self.entry_time = None

        self.sl_trigger = None
        self.sl_limit = None                # the limit paired with the trigger
        self.sl_order_id = None
        self.sl_verified = False            # this leg is considered protected
        # TRUE only when a REAL order is confirmed resting at the broker. A paper
        # stop sets sl_verified (so paper rehearses live) but NEVER this — the UI
        # keys its shield off this flag, so a shield on screen always means a stop
        # that genuinely exists in the broker's book.
        self.sl_at_broker = False
        self.sl_checked = None              # HH:MM:SS the broker last confirmed it

        self.exit_order_id = None
        self.exit_price = None
        self.exit_time = None
        self.exit_reason = None
        self.created = dt.datetime.now().strftime("%H:%M:%S")

    @property
    def symbol(self) -> str:
        """Alias kept because marks/ledger key on it; always the tradingsymbol."""
        return self.tradingsymbol

    # ── state ────────────────────────────────────────────────────────────
    @property
    def is_live(self) -> bool:
        """Short and still in the market (protected or not)."""
        return self.status in (OPEN, NAKED)

    @property
    def is_protected(self) -> bool:
        return self.status == OPEN and self.sl_verified

    def mark_filled(self, order_id: str, price: float, time_str: str = None):
        self.entry_order_id = order_id
        self.entry_price = price
        self.entry_time = time_str or dt.datetime.now().strftime("%H:%M:%S")
        self.status = NAKED                 # NOT open until the stop is confirmed

    def mark_protected(self, sl_order_id: str, trigger: float,
                       at_broker: bool = False, limit: float = None,
                       time_str: str = None):
        self.sl_order_id = sl_order_id
        self.sl_trigger = trigger
        self.sl_limit = limit
        self.sl_verified = True
        self.sl_at_broker = bool(at_broker)
        self.sl_checked = time_str or dt.datetime.now().strftime("%H:%M:%S")
        self.status = OPEN

    def mark_unprotected(self, time_str: str = None):
        """The stop is no longer confirmed at the broker (cancelled, rejected, or
        it vanished between polls). Drops the leg back to NAKED so the controller's
        protection invariant re-places it or covers the leg."""
        self.sl_verified = False
        self.sl_at_broker = False
        self.sl_checked = time_str or dt.datetime.now().strftime("%H:%M:%S")
        if self.status == OPEN:
            self.status = NAKED

    def mark_stopped(self, price: float, time_str: str = None):
        self.status = STOPPED
        self.exit_price = price
        self.exit_time = time_str or dt.datetime.now().strftime("%H:%M:%S")
        self.exit_reason = "stop loss"
        self.sl_verified = False
        self.sl_at_broker = False

    def mark_closed(self, price: float, reason: str, order_id: str = None,
                    time_str: str = None):
        self.status = CLOSED
        self.exit_price = price
        self.exit_order_id = order_id
        self.exit_time = time_str or dt.datetime.now().strftime("%H:%M:%S")
        self.exit_reason = reason
        self.sl_verified = False
        self.sl_at_broker = False

    def pnl(self, mark: float = None) -> float | None:
        """Rupees for this leg. Realized once closed/stopped, else marked to `mark`.
        A short profits when the premium falls, hence entry - exit."""
        if self.entry_price is None:
            return None
        out = self.exit_price if self.status in (STOPPED, CLOSED) else mark
        if out is None:
            return None
        return round((self.entry_price - out) * self.qty, 2)

    def to_dict(self, mark: float = None) -> dict:
        return {
            "opt_type": self.opt_type, "strike": self.strike,
            "symbol": self.symbol, "tradingsymbol": self.tradingsymbol,
            "qty": self.qty, "otm_level": self.otm_level, "reason": self.reason,
            "status": self.status, "entry_price": self.entry_price,
            "entry_time": self.entry_time, "entry_order_id": self.entry_order_id,
            "sl_trigger": self.sl_trigger, "sl_limit": self.sl_limit,
            "sl_order_id": self.sl_order_id, "sl_verified": self.sl_verified,
            "sl_at_broker": self.sl_at_broker, "sl_checked": self.sl_checked,
            "exit_price": self.exit_price, "exit_time": self.exit_time,
            "exit_reason": self.exit_reason,
            "mark": mark, "pnl": self.pnl(mark),
        }


class Position:
    """The current CE + PE legs plus every leg the day has been through.

    `ce` / `pe` are the CURRENT legs (or None when that side is out). Replaced
    legs move into `history`, so the UI can show the full adjustment trail and
    realized P&L is just the sum over history.
    """

    def __init__(self):
        self.ce: Leg | None = None
        self.pe: Leg | None = None
        self.history: list[Leg] = []

    # ── access ───────────────────────────────────────────────────────────
    def leg(self, opt_type: str) -> Leg | None:
        return self.ce if opt_type == CE else self.pe

    def set_leg(self, leg: Leg):
        if leg.opt_type == CE:
            self.ce = leg
        else:
            self.pe = leg

    def retire(self, opt_type: str):
        """Move a finished leg into history and clear that side."""
        leg = self.leg(opt_type)
        if leg is not None:
            self.history.append(leg)
            if opt_type == CE:
                self.ce = None
            else:
                self.pe = None

    def live_legs(self) -> list[Leg]:
        return [l for l in (self.ce, self.pe) if l is not None and l.is_live]

    def unprotected_legs(self) -> list[Leg]:
        """Legs that are short WITHOUT a confirmed stop. Must always be empty
        after the controller finishes a tick — this is the hard rule."""
        return [l for l in self.live_legs() if not l.sl_verified]

    # ── shape of the position ────────────────────────────────────────────
    @property
    def n_live(self) -> int:
        return len(self.live_legs())

    @property
    def is_complete(self) -> bool:
        """Both sides live — a real strangle."""
        return self.n_live == 2

    @property
    def is_single(self) -> bool:
        """One side live — deliberately allowed between a stop-out and the next
        adjustment window, and shown as a warning in the UI for exactly that long."""
        return self.n_live == 1

    @property
    def is_flat(self) -> bool:
        return self.n_live == 0

    def missing_side(self) -> str | None:
        """Which side needs re-entry when we are single-legged."""
        if not self.is_single:
            return None
        return PE if (self.ce is not None and self.ce.is_live) else CE

    # ── P&L ──────────────────────────────────────────────────────────────
    def realized(self) -> float:
        return round(sum(l.pnl() or 0 for l in self.history), 2)

    def unrealized(self, marks: dict) -> float:
        return round(sum(l.pnl(marks.get(l.symbol)) or 0 for l in self.live_legs()), 2)

    def mtm(self, marks: dict) -> float:
        return round(self.realized() + self.unrealized(marks), 2)

    def to_dict(self, marks: dict) -> dict:
        m = marks or {}
        return {
            "ce": self.ce.to_dict(m.get(self.ce.symbol)) if self.ce else None,
            "pe": self.pe.to_dict(m.get(self.pe.symbol)) if self.pe else None,
            "history": [l.to_dict() for l in self.history],
            "n_live": self.n_live, "is_complete": self.is_complete,
            "is_single": self.is_single, "is_flat": self.is_flat,
            "missing_side": self.missing_side(),
            "unprotected": [l.symbol for l in self.unprotected_legs()],
            "realized": self.realized(), "unrealized": self.unrealized(m),
            "mtm": self.mtm(m),
        }
