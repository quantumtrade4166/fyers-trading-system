"""
live/position.py — the leg model for a strangle whose strikes move during a cycle.
=================================================================================

Ported from the NSE delta-neutral strangle. The state machine is unchanged,
because it is the part that encodes the hard-won safety rules; what changes is
the money arithmetic, which on Delta runs through a CONTRACT VALUE.

    NSE     P&L = (entry - exit) * qty                 qty = lots * lot_size
    Delta   P&L = (entry - exit) * contracts * 0.001   0.001 BTC per contract

A premium quoted as $150 therefore costs $0.15 per contract, and 100 contracts of
it is $15. Getting that factor wrong is the kind of mistake that silently scales
a whole month's results by 1000x, so `contract_value` is stored ON THE LEG at
creation — read from the exchange's product master — rather than being reached
for from config at the moment of the calculation.

Leg lifecycle (identical to the NSE book):
    PENDING  -> placed, not yet filled
    OPEN     -> filled AND its stop is confirmed
    NAKED    -> filled but NOT protected. Never allowed to persist: the
                controller either places the stop or buys the leg back. It exists
                so the condition is nameable and visible instead of being an
                invisible gap between two lines of code.
    STOPPED  -> the stop fired and covered the leg
    CLOSED   -> deliberately bought back (adjustment or square-off)
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as dt

PENDING, OPEN, NAKED, STOPPED, CLOSED = "PENDING", "OPEN", "NAKED", "STOPPED", "CLOSED"
CE, PE = "CE", "PE"


class Leg:
    def __init__(self, opt_type: str, strike: float, symbol: str, contracts: int,
                 *, contract_value: float = 0.001, product_id: int = None,
                 otm_level: int = None, reason: str = None):
        self.opt_type = opt_type
        self.strike = float(strike)
        # Delta's own contract symbol (C-BTC-82000-040926) is the leg's identity
        # AND its order key — one field, because two that must be kept in sync is
        # a bug waiting to happen.
        self.symbol = symbol
        self.product_id = product_id
        self.contracts = int(contracts)
        self.contract_value = float(contract_value)
        self.otm_level = otm_level
        self.reason = reason               # why this strike was picked (for the log)

        self.status = PENDING
        self.entry_order_id = None
        self.entry_price = None
        self.entry_time = None
        self.entry_crossed = None          # did the entry fill against a real bid?

        self.sl_trigger = None
        self.sl_order_id = None
        self.sl_verified = False           # this leg is considered protected
        # TRUE only when a REAL order is confirmed resting at the exchange. A
        # paper stop sets sl_verified (so paper rehearses live) but NEVER this, so
        # a shield shown anywhere always means a stop that genuinely exists.
        self.sl_at_broker = False
        self.sl_checked = None

        self.exit_order_id = None
        self.exit_price = None
        self.exit_time = None
        self.exit_reason = None
        self.exit_crossed = None
        self.fees = 0.0                    # USD, accumulated across entry + exit
        self.created = dt.datetime.now().strftime("%H:%M:%S")

    @property
    def multiplier(self) -> float:
        """USD per point of premium for this whole leg."""
        return self.contracts * self.contract_value

    # ── state ────────────────────────────────────────────────────────────
    @property
    def is_live(self) -> bool:
        """Short and still in the market (protected or not)."""
        return self.status in (OPEN, NAKED)

    @property
    def is_protected(self) -> bool:
        return self.status == OPEN and self.sl_verified

    def mark_filled(self, order_id: str, price: float, time_str: str = None,
                    crossed: bool = None, fee: float = 0.0):
        self.entry_order_id = order_id
        self.entry_price = price
        self.entry_time = time_str or dt.datetime.now().strftime("%H:%M:%S")
        self.entry_crossed = crossed
        self.fees += float(fee or 0.0)
        self.status = NAKED                # NOT open until the stop is confirmed

    def mark_protected(self, sl_order_id: str, trigger: float,
                       at_broker: bool = False, time_str: str = None):
        self.sl_order_id = sl_order_id
        self.sl_trigger = trigger
        self.sl_verified = True
        self.sl_at_broker = bool(at_broker)
        self.sl_checked = time_str or dt.datetime.now().strftime("%H:%M:%S")
        self.status = OPEN

    def mark_unprotected(self, time_str: str = None):
        """The stop is no longer confirmed. Drops the leg back to NAKED so the
        controller's protection invariant re-places it or covers the leg."""
        self.sl_verified = False
        self.sl_at_broker = False
        self.sl_checked = time_str or dt.datetime.now().strftime("%H:%M:%S")
        if self.status == OPEN:
            self.status = NAKED

    def mark_stopped(self, price: float, time_str: str = None, fee: float = 0.0,
                     crossed: bool = None):
        self.status = STOPPED
        self.exit_price = price
        self.exit_time = time_str or dt.datetime.now().strftime("%H:%M:%S")
        self.exit_reason = "stop loss"
        self.exit_crossed = crossed
        self.fees += float(fee or 0.0)
        self.sl_verified = False
        self.sl_at_broker = False

    def mark_closed(self, price: float, reason: str, order_id: str = None,
                    time_str: str = None, fee: float = 0.0, crossed: bool = None):
        self.status = CLOSED
        self.exit_price = price
        self.exit_order_id = order_id
        self.exit_time = time_str or dt.datetime.now().strftime("%H:%M:%S")
        self.exit_reason = reason
        self.exit_crossed = crossed
        self.fees += float(fee or 0.0)
        self.sl_verified = False
        self.sl_at_broker = False

    # ── money ────────────────────────────────────────────────────────────
    def gross_pnl(self, mark: float = None) -> float | None:
        """USD before fees. Realized once closed/stopped, else marked to `mark`.
        A short profits when the premium falls, hence entry - exit."""
        if self.entry_price is None:
            return None
        out = self.exit_price if self.status in (STOPPED, CLOSED) else mark
        if out is None:
            return None
        return round((self.entry_price - out) * self.multiplier, 4)

    def pnl(self, mark: float = None) -> float | None:
        """USD AFTER fees — the only P&L any caller should quote.

        Fees are charged on both sides of a round trip, and this strategy re-legs
        every time the 2x rule fires. Over a month the three profiles trade very
        different numbers of times, so quoting gross would not just flatter them
        all, it could reverse their ranking.
        """
        g = self.gross_pnl(mark)
        return None if g is None else round(g - self.fees, 4)

    def to_dict(self, mark: float = None) -> dict:
        return {
            "opt_type": self.opt_type, "strike": self.strike, "symbol": self.symbol,
            "product_id": self.product_id, "contracts": self.contracts,
            "contract_value": self.contract_value, "otm_level": self.otm_level,
            "reason": self.reason, "status": self.status,
            "entry_price": self.entry_price, "entry_time": self.entry_time,
            "entry_order_id": self.entry_order_id, "entry_crossed": self.entry_crossed,
            "sl_trigger": self.sl_trigger, "sl_order_id": self.sl_order_id,
            "sl_verified": self.sl_verified, "sl_at_broker": self.sl_at_broker,
            "sl_checked": self.sl_checked, "exit_price": self.exit_price,
            "exit_time": self.exit_time, "exit_reason": self.exit_reason,
            "exit_crossed": self.exit_crossed, "fees": round(self.fees, 4),
            "mark": mark, "gross_pnl": self.gross_pnl(mark), "pnl": self.pnl(mark),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Leg":
        """Rebuild a leg from its own serialised form, so a paper book survives an
        engine restart. A LIVE book must never trust a file for what it holds — it
        reconciles against the exchange, the only authority on a real position."""
        leg = cls(d["opt_type"], d["strike"], d.get("symbol"), d["contracts"],
                  contract_value=d.get("contract_value", 0.001),
                  product_id=d.get("product_id"), otm_level=d.get("otm_level"),
                  reason=d.get("reason"))
        leg.status = d.get("status", leg.status)
        leg.entry_price = d.get("entry_price")
        leg.entry_time = d.get("entry_time")
        leg.entry_order_id = d.get("entry_order_id")
        leg.entry_crossed = d.get("entry_crossed")
        leg.sl_trigger = d.get("sl_trigger")
        leg.sl_order_id = d.get("sl_order_id")
        leg.sl_verified = bool(d.get("sl_verified"))
        leg.sl_at_broker = bool(d.get("sl_at_broker"))
        leg.sl_checked = d.get("sl_checked")
        leg.exit_price = d.get("exit_price")
        leg.exit_time = d.get("exit_time")
        leg.exit_reason = d.get("exit_reason")
        leg.exit_crossed = d.get("exit_crossed")
        leg.fees = float(d.get("fees") or 0.0)
        return leg


class Position:
    """The current CE + PE legs plus every leg the cycle has been through.

    `ce` / `pe` are the CURRENT legs (None when that side is out). Replaced legs
    move into `history`, so the adjustment trail is inspectable and realized P&L
    is just the sum over history.
    """

    def __init__(self):
        self.ce: Leg | None = None
        self.pe: Leg | None = None
        self.history: list = []

    # ── access ───────────────────────────────────────────────────────────
    def leg(self, opt_type: str):
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

    def live_legs(self) -> list:
        return [l for l in (self.ce, self.pe) if l is not None and l.is_live]

    def unprotected_legs(self) -> list:
        """Legs short WITHOUT a confirmed stop. Must always be empty once the
        controller finishes a tick — this is the hard rule."""
        return [l for l in self.live_legs() if not l.sl_verified]

    # ── shape ────────────────────────────────────────────────────────────
    @property
    def n_live(self) -> int:
        return len(self.live_legs())

    @property
    def is_complete(self) -> bool:
        return self.n_live == 2

    @property
    def is_single(self) -> bool:
        """One side live — deliberately allowed between a stop-out and the next
        adjustment window, and surfaced as a warning for exactly that long."""
        return self.n_live == 1

    @property
    def is_flat(self) -> bool:
        return self.n_live == 0

    def missing_side(self):
        if not self.is_single:
            return None
        return PE if (self.ce is not None and self.ce.is_live) else CE

    # ── P&L (USD, net of fees) ───────────────────────────────────────────
    def realized(self) -> float:
        return round(sum(l.pnl() or 0 for l in self.history), 4)

    def unrealized(self, marks: dict) -> float:
        return round(sum(l.pnl(marks.get(l.symbol)) or 0 for l in self.live_legs()), 4)

    def mtm(self, marks: dict) -> float:
        return round(self.realized() + self.unrealized(marks), 4)

    def fees_paid(self) -> float:
        return round(sum(l.fees for l in self.history + self.live_legs()), 4)

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
            "mtm": self.mtm(m), "fees": self.fees_paid(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        pos = cls()
        if d.get("ce"):
            pos.ce = Leg.from_dict(d["ce"])
        if d.get("pe"):
            pos.pe = Leg.from_dict(d["pe"])
        pos.history = [Leg.from_dict(h) for h in (d.get("history") or [])]
        return pos
