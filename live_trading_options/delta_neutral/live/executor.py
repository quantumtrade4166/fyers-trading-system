"""
live/executor.py — order placement, paper and live behind one interface.
========================================================================

The controller expresses intent ("sell this leg", "protect it", "cover it") and
never branches on paper-vs-live. That branch lives here, once, so the strategy
logic that decides WHAT to trade is identical in both modes and paper genuinely
rehearses live.

LIVE goes through the existing, battle-tested
`strangle_strategy/live/kite_executor.py` — the same contract resolution from
Kite's own instrument dump, the same marketable-LIMIT entries (Zerodha rejects
MARKET on options), and the same place-verified pattern that survives a lost HTTP
response without double-placing. Orders carry tag `dnstrangle`, keeping this
strategy's own book scoped separately from the VWAP strangle's `vwstrangle`.

PAPER fills at the current mark and fakes an order id. Paper stops are not placed
anywhere — the controller simulates them from ticks.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import time
import datetime as dt

from live.broker import kite_executor as kx, TAG

BUY, SELL = "BUY", "SELL"

# marketable-limit buffer: price THROUGH the touch so a LIMIT fills like a market
# order. Only a worst-case cap, never the expected fill. Escalates on a retry.
_MKT_BUFS = (0.30, 0.60, 0.90)
_FILL_POLL_SECONDS = 5.0


class Fill:
    def __init__(self, order_id: str, price: float, time_str: str, status: str = "COMPLETE",
                 message: str = None):
        self.order_id, self.price, self.time, self.status = order_id, price, time_str, status
        # why it failed, verbatim from the broker. A rejection has to travel back to
        # the strategy as DATA — raising unwinds the tick and the strategy never
        # learns its own order was refused.
        self.message = message

    @property
    def margin(self) -> bool:
        """The order was refused for want of funds."""
        return kx.is_margin_error(self.message or "")

    @property
    def ok(self) -> bool:
        return self.status == "COMPLETE" and self.price is not None

    def __repr__(self):
        return f"Fill({self.order_id} {self.status} @{self.price})"


class Executor:
    def __init__(self, index: str, expiry, *, tag: str = TAG,
                 kite=None, live: bool = False, product: str = "NRML", clock=None,
                 sl_buffer: float = 2.0):
        self.index, self.expiry, self.tag = index, expiry, tag
        self.kite, self.product = kite, product
        # points between the SL trigger and its limit (trigger 40 -> limit 42), so
        # the stop actually fills instead of being jumped by the move it exists for
        self.sl_buffer = float(sl_buffer)
        self._live = live
        self._paper_seq = 0
        self._resolved: dict[tuple, dict] = {}     # (strike, type) -> kite contract
        # Paper fills are stamped with the STRATEGY's clock, not the wall clock.
        # Live fills always use the broker's own exchange timestamp instead. Without
        # this a paper run (or a replay) labels every fill with the moment the
        # script happened to run, which makes the trade log unreadable.
        self._clock = clock or (lambda: dt.datetime.now().strftime("%H:%M:%S"))

    # ── mode ─────────────────────────────────────────────────────────────
    def set_live(self, live: bool):
        self._live = bool(live)

    @property
    def is_live(self) -> bool:
        return self._live and self.kite is not None

    @property
    def broker_ready(self) -> bool:
        return self.kite is not None

    @property
    def exchange(self) -> str:
        """Options exchange for this index (NIFTY -> NFO, SENSEX -> BFO)."""
        return kx.EXCHANGE[self.index]

    # ── contract resolution (never hand-build a tradingsymbol) ───────────
    def resolve(self, strike: int, opt_type: str) -> dict | None:
        """Kite contract for a strike, from the broker's own instrument dump.
        Cached — the dump is large and identical for every strike of the day."""
        key = (int(strike), opt_type)
        if key in self._resolved:
            return self._resolved[key]
        if self.kite is None:
            return None
        try:
            c = kx.resolve(self.kite, self.index, self.expiry, strike, opt_type)
            self._resolved[key] = c
            return c
        except Exception:
            return None

    # ── paper helpers ────────────────────────────────────────────────────
    def _paper_fill(self, kind: str, price: float) -> Fill:
        self._paper_seq += 1
        return Fill(f"paper-{kind}-{self._paper_seq}", price, self._clock())

    # ── entries / exits ──────────────────────────────────────────────────
    def sell(self, leg, mark: float) -> Fill:
        """Short one leg. Returns a Fill; `Fill.ok` is False if it never filled."""
        if not self.is_live:
            return self._paper_fill("sell", mark)
        return self._live_order(leg, SELL, mark, "entry")

    def buy(self, leg, mark: float, kind: str = "exit", urgency: float = 1.0) -> Fill:
        """Cover one leg. `urgency` widens how far through the book we are willing
        to reach — used by the retry loop when a cover will not fill."""
        if not self.is_live:
            return self._paper_fill("buy", mark)
        return self._live_order(leg, BUY, mark, kind, urgency=urgency)

    def _limit_price(self, side: str, mark: float, buf: float) -> float:
        if not mark or mark <= 0:                  # no mark yet → uncapped marketable
            return 0.05 if side == SELL else 100000.0
        return mark * (1 - buf) if side == SELL else mark * (1 + buf)

    def _live_order(self, leg, side: str, mark: float, kind: str,
                    urgency: float = 1.0) -> Fill:
        """Place a marketable LIMIT and poll for the fill, escalating on a laggard.
        Cancels anything that will not fill so nothing rests unnoticed.

        Each rung is priced off the ACTUAL order book when it can be read: the
        price that clears our size against the visible levels, plus a few ticks.
        A multiple of the last mark is only the fallback — it is a guess that can
        be far too tight (never fills) or far too wide (sweeps a thin book)."""
        import time
        last_oid = None
        for attempt, buf in enumerate(_MKT_BUFS):
            # cushion grows with the rung AND with urgency, but in TICKS against
            # real levels rather than as a percentage of a stale mark
            cushion = int((2 + attempt * 4) * max(1.0, urgency))
            price = kx.marketable_price(
                self.kite, leg.exchange, leg.tradingsymbol, side, leg.qty,
                fallback=self._limit_price(side, mark, buf), cushion_ticks=cushion)
            try:
                oid = kx.place_limit_verified(self.kite, leg.tradingsymbol, leg.exchange,
                                              side, leg.qty, price,
                                              leg.product or self.product, self.tag)
            except Exception as e:
                # REJECTED comes back as a Fill, not an exception. A raise here
                # unwinds the whole tick: the strategy never logs it, the snapshot
                # never shows it, and a half-adjusted position is left running.
                msg = f"{type(e).__name__}: {e}"
                if kx.is_margin_error(e) or attempt == len(_MKT_BUFS) - 1:
                    return Fill(None, None, None, status="REJECTED", message=msg)
                continue                      # transient — try the next rung
            last_oid = oid
            deadline = time.monotonic() + (_FILL_POLL_SECONDS if attempt == 0 else 3.0)
            while time.monotonic() < deadline:
                st = kx.order_status(self.kite, oid)
                if st["status"] == "COMPLETE":
                    return Fill(oid, st.get("avg_price"), st.get("fill_time"))
                if st["status"] in ("REJECTED", "CANCELLED"):
                    break
                time.sleep(0.4)
            # not filled — cancel before re-pricing so we never hold two live orders
            try:
                kx.cancel(self.kite, oid)
            except Exception:
                pass
            st = kx.order_status(self.kite, oid)
            if st["status"] == "COMPLETE":          # filled during the cancel race
                return Fill(oid, st.get("avg_price"), st.get("fill_time"))
        return Fill(last_oid, None, None, status="NOFILL",
                    message="no fill after the full price ladder")

    # ── protective stops ─────────────────────────────────────────────────
    def place_stop(self, leg, trigger: float) -> tuple[str | None, bool, bool, float]:
        """Place the resting stop for a short leg and CONFIRM it at the exchange.

        Returns (order_id, protected, at_broker, limit_price).
          protected  — the leg may be treated as covered by a stop
          at_broker  — a REAL order is confirmed resting in the broker's book

        The two are the same thing in live mode. In PAPER they deliberately differ:
        the stop is simulated so paper rehearses live, but `at_broker` stays False
        so the terminal never shows a shield for a stop that does not exist. That
        distinction is the whole point — a shield on screen must always mean a stop
        the broker is actually holding.
        """
        limit = kx.sl_limit_price(trigger, BUY, self.sl_buffer)
        if not self.is_live:
            self._paper_seq += 1
            return f"paper-sl-{self._paper_seq}", True, False, limit
        import time
        try:
            oid = kx.place_sl_verified(self.kite, leg.tradingsymbol, leg.exchange,
                                       BUY, leg.qty, trigger,
                                       leg.product or self.product, self.tag,
                                       buffer=self.sl_buffer)
        except Exception:
            return None, False, False, limit
        # confirm it is actually resting — a stop that was rejected protects nothing
        for _ in range(8):
            if kx.is_resting(self.kite, oid):
                return oid, True, True, limit
            st = kx.order_status(self.kite, oid)
            if st["status"] in ("REJECTED", "CANCELLED"):
                return oid, False, False, limit
            if st["status"] == "COMPLETE":          # triggered instantly (already past it)
                return oid, False, False, limit
            time.sleep(0.4)
        return oid, False, False, limit

    def modify_stop(self, leg, trigger: float) -> tuple[bool, float]:
        """Move a leg's resting stop to a tighter trigger. Returns (ok, new_limit).

        Modify rather than cancel-and-replace, so the leg is never unprotected for
        even a moment. If the broker refuses the modify the caller keeps the OLD
        stop — which is still a valid stop — rather than ending up with none."""
        limit = kx.sl_limit_price(trigger, BUY, self.sl_buffer)
        if not self.is_live or leg.sl_order_id is None \
                or str(leg.sl_order_id).startswith("paper"):
            return True, limit                       # paper stop is simulated
        try:
            kx.modify_sl(self.kite, leg.sl_order_id, trigger, self.sl_buffer)
        except Exception:
            return False, limit
        # confirm it is still resting after the change
        for _ in range(6):
            if kx.is_resting(self.kite, leg.sl_order_id):
                return True, limit
            st = kx.order_status(self.kite, leg.sl_order_id)
            if st["status"] in ("REJECTED", "CANCELLED", "COMPLETE"):
                return False, limit
            time.sleep(0.3)
        return False, limit

    def cancel_stop(self, leg) -> bool:
        """Cancel a leg's resting stop before covering it deliberately. A stop left
        behind after the leg is closed would fire later and open a NEW naked long."""
        if leg.sl_order_id is None:
            return True
        if not self.is_live or str(leg.sl_order_id).startswith("paper"):
            return True
        try:
            kx.cancel(self.kite, leg.sl_order_id)
        except Exception:
            pass
        # confirm it is gone; if it FILLED during the race the leg is already covered
        st = kx.order_status(self.kite, leg.sl_order_id)
        return st["status"] in ("CANCELLED", "REJECTED", "COMPLETE")

    def stop_fill(self, leg) -> Fill | None:
        """If a leg's resting stop has fired, the Fill that covered it, else None."""
        if leg.sl_order_id is None or not self.is_live:
            return None
        if str(leg.sl_order_id).startswith("paper"):
            return None
        st = kx.order_status(self.kite, leg.sl_order_id)
        if st["status"] == "COMPLETE":
            return Fill(leg.sl_order_id, st.get("avg_price"), st.get("fill_time"))
        return None
