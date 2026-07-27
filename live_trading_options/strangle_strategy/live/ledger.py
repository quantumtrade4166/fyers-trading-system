"""
live/ledger.py — the strategy's OWN book.
=========================================

The single source of truth for what THIS strategy intends to hold and what it has
actually filled — INDEPENDENT of the broker's netted position.

Why: on Zerodha, positions net by tradingsymbol. If the strategy is short 5 and you
manually buy 2 of the same strike, the broker shows net -3. If the strategy ever
sized its exit off that -3 it would mis-cover. So every decision here (how many to
buy back, current short qty, P&L) is computed from the strategy's OWN orders only.
The broker is used only to VERIFY this book (reconcile) and, at flatten time, for
the true filled qty.

Quantities are in UNITS (lots x lot_size, e.g. 1 NIFTY lot = 65). Prices are the
per-unit option premium. Cash and P&L are in rupees.
"""

import datetime as dt

SELL, BUY = "SELL", "BUY"
PENDING, COMPLETE, PARTIAL, REJECTED, CANCELLED = (
    "PENDING", "COMPLETE", "PARTIAL", "REJECTED", "CANCELLED")


class Order:
    """One leg order the strategy placed. Intended qty is fixed at creation; the
    fill (status / filled_qty / avg_price) is updated as the broker reports back."""

    def __init__(self, order_id: str, symbol: str, side: str, qty: int,
                 cycle: int, kind: str):
        self.order_id = order_id      # broker order id (or a paper id)
        self.symbol = symbol
        self.side = side              # SELL / BUY
        self.qty = qty                # INTENDED units (lots * lot_size)
        self.cycle = cycle            # cycle number 1..N
        self.kind = kind              # entry / exit / square_off / naked_cover
        self.status = PENDING
        self.filled_qty = 0
        self.avg_price = None
        self.ts = dt.datetime.now().isoformat(timespec="seconds")

    def to_dict(self) -> dict:
        return {"order_id": self.order_id, "symbol": self.symbol, "side": self.side,
                "qty": self.qty, "cycle": self.cycle, "kind": self.kind,
                "status": self.status, "filled_qty": self.filled_qty,
                "avg_price": self.avg_price, "ts": self.ts}


class Ledger:
    def __init__(self):
        self.orders: dict[str, Order] = {}      # order_id -> Order

    # ── recording ─────────────────────────────────────────────────────────
    def record(self, order: Order) -> Order:
        self.orders[order.order_id] = order
        return order

    def update_fill(self, order_id: str, status: str,
                    filled_qty: int = None, avg_price: float = None) -> Order:
        o = self.orders[order_id]
        o.status = status
        if filled_qty is not None:
            o.filled_qty = filled_qty
        if avg_price is not None:
            o.avg_price = avg_price
        if status == COMPLETE and filled_qty is None:
            o.filled_qty = o.qty                 # a COMPLETE order filled its full qty
        return o

    # ── the OWN book (from fills only) ────────────────────────────────────
    def _filled(self):
        return [o for o in self.orders.values()
                if o.status in (COMPLETE, PARTIAL) and o.avg_price is not None]

    def open_short(self, symbol: str) -> int:
        """Units the strategy is still SHORT in `symbol` = filled sells - filled buys.
        This is what the exit must buy back — NOT the broker's net position."""
        sold = sum(o.filled_qty for o in self._filled() if o.symbol == symbol and o.side == SELL)
        bought = sum(o.filled_qty for o in self._filled() if o.symbol == symbol and o.side == BUY)
        return sold - bought

    def open_shorts(self) -> dict[str, int]:
        syms = {o.symbol for o in self._filled()}
        return {s: q for s in syms if (q := self.open_short(s)) > 0}

    # ── reconciliation helpers for the guard ─────────────────────────────
    # The real "reconcile against the broker" is done by the executor polling each
    # order_id's status and calling update_fill() — so open_short() below always
    # reflects broker-confirmed reality (never the netted position). These helpers
    # let the guard spot trouble.
    def pending_orders(self) -> list[Order]:
        """Orders not yet in a terminal state — the executor must keep polling
        these; the guard must not assume they filled."""
        return [o for o in self.orders.values() if o.status in (PENDING, PARTIAL)]

    def rejected_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.status == REJECTED]

    def is_balanced(self, ce_sym: str, pe_sym: str) -> bool:
        """A short strangle must be equally short on BOTH legs. If not, one leg is
        naked (e.g. an exit filled one side but the other rejected)."""
        return self.open_short(ce_sym) == self.open_short(pe_sym)

    def naked_leg(self, ce_sym: str, pe_sym: str):
        """If the two legs are imbalanced, return (symbol, extra_short_units) for the
        MORE-short (uncovered) leg — the qty the guard must immediately buy back to
        remove the naked exposure. Returns None when balanced."""
        dce, dpe = self.open_short(ce_sym), self.open_short(pe_sym)
        if dce == dpe:
            return None
        return (ce_sym, dce - dpe) if dce > dpe else (pe_sym, dpe - dce)

    # ── P&L (marked to `marks`; realized when flat) ───────────────────────
    def cash(self) -> float:
        """Net cash from the strategy's OWN fills (+ on sells, - on buys)."""
        return round(sum((1 if o.side == SELL else -1) * o.avg_price * o.filled_qty
                         for o in self._filled()), 2)

    def mtm(self, marks: dict[str, float]) -> float:
        """Total P&L (realized + unrealized): own cash, minus the cost to buy back
        any still-open shorts at the current mark. When flat this equals realized."""
        cover = sum(marks.get(s, 0.0) * q for s, q in self.open_shorts().items())
        return round(self.cash() - cover, 2)

    def snapshot(self) -> dict:
        return {"orders": [o.to_dict() for o in self.orders.values()],
                "open_shorts": self.open_shorts(), "cash": self.cash()}
