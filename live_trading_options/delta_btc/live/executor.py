"""
live/executor.py — the paper fill simulator, and where the live path will go.
=============================================================================

The controller expresses intent ("sell this leg", "protect it", "cover it") and
never branches on paper-vs-live. That branch lives here, once, so the logic that
decides WHAT to trade is identical in both modes and paper genuinely rehearses
live.

v1 IS PAPER ONLY. `is_live` can never be True yet: `place_live_order` raises on
purpose. That is deliberate — a month of paper is the point, and a half-built
live path that could be switched on by a config typo is exactly how the NSE book
lost a morning. When live is built it goes here, behind a signed REST client,
rehearsed on testnet first.

HOW A PAPER FILL IS PRICED
A SELL fills at the best BID and a BUY lifts the best ASK — never at the mark.
Filling at the mark hands the book half the spread on every single leg, and this
strategy re-legs every time the 2x rule fires; across a month that alone could
decide which session profile "wins". Where a side of the book is empty the mark
is used and the fill is FLAGGED (`crossed=False`) so it is visible in the trade
log rather than silently counted as real.

Depth is deliberately NOT walked. At 100 contracts against the 7,000-contract
top-of-book seen on these strikes, the whole order rests inside level one; adding
a depth model would be inventing precision the size does not need.

FEES
Delta charges the LOWER of 0.01% of notional and 3.5% of premium, per side.
Both are modelled. The three profiles trade very different numbers of times, so
fees are exactly the kind of thing that could reverse the ranking, and a paper
month without them would be worthless for the comparison it exists to make.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

BUY, SELL = "BUY", "SELL"


class Fill:
    def __init__(self, order_id, price, time_str, status="COMPLETE",
                 message=None, crossed=None, fee=0.0):
        self.order_id, self.price, self.time = order_id, price, time_str
        self.status = status
        # why it failed, verbatim. A rejection has to travel back to the strategy
        # as DATA — raising unwinds the tick and the strategy never learns its own
        # order was refused, which is precisely the 25-Aug NSE failure.
        self.message = message
        self.crossed = crossed
        self.fee = float(fee or 0.0)

    @property
    def ok(self) -> bool:
        return self.status == "COMPLETE" and self.price is not None

    def __repr__(self):
        return f"Fill({self.order_id} {self.status} @{self.price} fee {self.fee:.4f})"


class Executor:
    def __init__(self, profile_name: str, chain, *, fees: dict = None,
                 cross_the_spread: bool = True, clock=None, live: bool = False):
        self.profile = profile_name
        self.chain = chain
        self._live = bool(live)
        self.cross = bool(cross_the_spread)
        f = fees or {}
        self.taker_rate = float(f.get("taker_rate_notional", 0.0001))
        self.premium_cap = float(f.get("premium_cap_rate", 0.035))
        self.settle_rate = float(f.get("settlement_rate_notional", 0.0001))
        self._seq = 0
        # Paper fills are stamped with the STRATEGY's clock, not the wall clock.
        # Without this a replay labels every fill with the moment the script
        # happened to run, which makes the trade log unreadable.
        self._clock = clock or (lambda: "")

    # ── mode ─────────────────────────────────────────────────────────────
    @property
    def is_live(self) -> bool:
        return self._live

    def _next_id(self, kind: str) -> str:
        self._seq += 1
        return f"paper-{self.profile}-{kind}-{self._seq}"

    # ── fees ─────────────────────────────────────────────────────────────
    def fee_for(self, premium: float, contracts: int, contract_value: float,
                spot: float = None) -> float:
        """USD charged on one side of one leg.

        Delta takes the LOWER of 0.01% of notional and 3.5% of the premium paid.
        Deep OTM options are where the cap bites: at a $40 premium the notional
        fee is $0.008 while 3.5% of premium is $0.14, so notional wins — but on a
        near-the-money leg the cap is what applies, and the two cross inside the
        range this strategy actually trades.
        """
        if premium is None:
            return 0.0
        qty = contracts * contract_value
        premium_fee = self.premium_cap * premium * qty
        if spot:
            return round(min(self.taker_rate * spot * qty, premium_fee), 6)
        return round(premium_fee, 6)

    # ── entries / exits ──────────────────────────────────────────────────
    def sell(self, leg, kind: str = "entry") -> Fill:
        """Short one leg at the best bid."""
        return self._paper_order(leg, SELL, kind)

    def buy(self, leg, kind: str = "exit") -> Fill:
        """Cover one leg at the best ask."""
        return self._paper_order(leg, BUY, kind)

    def _paper_order(self, leg, side: str, kind: str) -> Fill:
        if self._live:
            return self.place_live_order(leg, side, kind)
        price, crossed = self.chain.fill_price(leg.strike, leg.opt_type, side)
        if not self.cross:
            price = self.chain.mark.get((leg.strike, leg.opt_type))
            crossed = False
        if price is None:
            # No price at all is a REFUSAL, not a zero. Returning a Fill rather
            # than raising is what lets the controller see its own failure and
            # unwind the other leg instead of leaving half a strangle standing.
            return Fill(None, None, self._clock(), status="NOFILL",
                        message=f"no book and no mark for {leg.symbol}")
        fee = self.fee_for(price, leg.contracts, leg.contract_value, self.chain.spot)
        return Fill(self._next_id(kind), float(price), self._clock(),
                    crossed=crossed, fee=fee)

    def place_live_order(self, leg, side: str, kind: str) -> Fill:
        """The live path. NOT BUILT — and raising is the point.

        Everything above this line is a simulation. If a config flag ever flips
        `live` on before this is implemented and rehearsed on testnet, the right
        outcome is a loud crash at the first order, not a silent paper fill that
        the operator reads as a real one.
        """
        raise NotImplementedError(
            "live order placement is not implemented — this package is paper-only "
            "in v1. Build the signed REST client and rehearse on testnet first.")

    # ── protective stops ─────────────────────────────────────────────────
    def place_stop(self, leg, trigger: float):
        """Arm this leg's stop. Returns (order_id, protected, at_broker).

        In paper the stop is SIMULATED — the controller watches the mark against
        the trigger every poll and fires it. `at_broker` stays False, because
        nothing is resting anywhere, and the snapshot keys its shield off that
        flag so it can never claim protection that does not exist.

        Delta does support a genuine resting stop (`stop_order_type` with a
        `stop_price` and a `mark_price` trigger), and it supports stop-MARKET,
        which Zerodha refuses on options — so the live version of this is simpler
        than the NSE one, not harder: no SL-limit buffer and no watchdog for a
        trigger the book gapped straight through.
        """
        if self._live:
            raise NotImplementedError("live stops are not implemented in v1")
        self._seq += 1
        return f"paper-{self.profile}-sl-{self._seq}", True, False

    def cancel_stop(self, leg) -> bool:
        """Nothing rests in paper, so there is nothing to cancel. Kept so the
        controller's flow is identical in both modes."""
        if self._live:
            raise NotImplementedError("live stop cancel is not implemented in v1")
        return True

    def stop_triggered(self, leg, mark: float) -> bool:
        """Has this leg's simulated stop fired?

        Compared against the MARK, matching what `stop_trigger_method:
        "mark_price"` would do live — Delta's mark is its own fair value and is
        what margin and liquidation use, so it is both the most robust trigger
        and the one a live order would actually be measured against. Triggering
        off the ask instead would fire on a single wide quote.
        """
        if leg.sl_trigger is None or mark is None:
            return False
        return mark >= leg.sl_trigger
