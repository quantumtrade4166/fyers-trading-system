"""
core/chain.py — one expiry's live BTC option chain.
===================================================

Holds a single expiry in memory and refreshes it from Delta's `/v2/tickers`.
Everything downstream — strike selection, leg marks, the fill simulator, the
snapshot the comparison tool reads — goes through here, so there is exactly one
idea of "the current premium" and one idea of "the current spot".

WHY REST POLLING AND NOT A WEBSOCKET
Delta publishes a perfectly good `ticker` websocket channel, and the NSE engine
this is ported from consumes a Kite socket. Here a poll is the better tool:

  - ONE unauthenticated call returns the ENTIRE BTC chain — every strike, both
    sides, mark and both sides of the touch, IV, greeks, OI and the spot index.
    A socket would need a subscription list that has to be extended as spot
    drifts, which is a whole class of "went blind after a big move" bug.
  - the strategy's tightest decision is a 60-second window. A 5-second refresh is
    already an order of magnitude finer than anything it acts on.
  - 3 rate-limit units per call against 20,000 per 5 minutes: polling every 5
    seconds spends about 3.6% of the budget.
  - no socket to silently stall at 04:00 with a position on. A failed poll is
    visible and self-healing; a dead socket is neither.

TWO PRICES, DELIBERATELY KEPT APART
  `mark`      what the strategy DECIDES on — Delta's own fair value, the number
              margin and liquidation are computed against
  `bid`/`ask` what a paper order FILLS at — a sell hits the bid, a buy lifts the
              ask
Deciding on the mark and filling at the mark would quietly hand the paper book
half the spread on every leg, and this strategy re-legs often enough for that
alone to decide the month.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as dt

from core.api import btc_option_tickers, btc_options
from core.selector import atm_strike, CE, PE

CONTRACT_VALUE = 0.001          # BTC per contract; overridden from the product master


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def expiry_code(d: dt.date) -> str:
    """Delta's expiry suffix: 4 Sep 2026 -> '040926'."""
    return f"{d.day:02d}{d.month:02d}{d.year % 100:02d}"


def parse_expiry(code: str) -> dt.date:
    return dt.date(2000 + int(code[4:6]), int(code[2:4]), int(code[0:2]))


def settlement_dt(code: str, settle_ist: str = "17:30") -> dt.datetime:
    """When this expiry settles, in IST."""
    h, m = (int(x) for x in settle_ist.split(":"))
    return dt.datetime.combine(parse_expiry(code), dt.time(h, m))


def available_expiries(rows: list = None) -> list:
    """Every listed BTC option expiry code, soonest first."""
    rows = rows if rows is not None else btc_options()
    codes = {r["symbol"].split("-")[-1] for r in rows
             if len(r.get("symbol", "").split("-")) == 4}
    return sorted(codes, key=parse_expiry)


def nearest_expiry(now: dt.datetime, rows: list = None,
                   settle_ist: str = "17:30") -> str | None:
    """The front expiry that has NOT yet settled at `now`.

    One rule serves every session profile, which is why it is a rule and not a
    per-profile setting: profile A entering at 09:30 gets today's 17:30 expiry;
    profiles B and C entering at 17:35 get tomorrow's, because today's settled
    five minutes earlier. Both are just "the next one that is still alive".
    """
    for code in available_expiries(rows):
        if settlement_dt(code, settle_ist) > now:
            return code
    return None


class LiveChain:
    """One expiry, kept current from ticker snapshots."""

    def __init__(self, expiry: str, span: int = 20, settle_ist: str = "17:30"):
        self.expiry = expiry
        self.span = span
        self.settle_ist = settle_ist
        self.spot: float | None = None
        self.atm: float | None = None
        self.updated: str | None = None
        self.contract_value = CONTRACT_VALUE

        self.mark: dict[tuple, float] = {}       # (strike, type) -> fair value
        self.bid: dict[tuple, float] = {}        # (strike, type) -> best bid
        self.ask: dict[tuple, float] = {}        # (strike, type) -> best ask
        self.by_key: dict[tuple, dict] = {}      # (strike, type) -> contract meta
        self.strikes: list[float] = []

    # ── build from the product master ────────────────────────────────────
    def load(self, rows: list = None):
        """Resolve every contract of this expiry once. Symbols and product ids
        come from Delta's own master and are never hand-built, so the thing we
        price is by construction the thing we would trade."""
        rows = rows if rows is not None else btc_options()
        mine = [r for r in rows if r.get("symbol", "").endswith(f"-{self.expiry}")]
        if not mine:
            raise RuntimeError(f"no live BTC contracts for expiry {self.expiry}")
        for r in mine:
            sym = r["symbol"]
            key = (float(r["strike_price"]), CE if sym.startswith("C-") else PE)
            self.by_key[key] = {
                "symbol": sym,
                # /v2/products calls it `id`; /v2/tickers calls the same number
                # `product_id`. Accept either so a chain can be built from
                # whichever payload the caller already had in hand.
                "product_id": int(r.get("id") or r["product_id"]),
                "tick_size": _f(r.get("tick_size")) or 0.1,
                "contract_value": _f(r.get("contract_value")) or CONTRACT_VALUE,
            }
        self.strikes = sorted({k[0] for k in self.by_key})
        self.contract_value = self.by_key[next(iter(self.by_key))]["contract_value"]
        return self

    # ── absorb a ticker snapshot ─────────────────────────────────────────
    def refresh(self, tickers: list = None) -> int:
        """Update marks and the touch from one ticker snapshot. Returns how many
        contracts of THIS expiry were updated."""
        tickers = tickers if tickers is not None else btc_option_tickers()
        n = 0
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith(f"-{self.expiry}"):
                if self.spot is None:
                    # the spot index is carried on every row, so any row will do
                    self.spot = _f(t.get("spot_price")) or self.spot
                continue
            strike = _f(t.get("strike_price"))
            if strike is None:
                continue
            key = (strike, CE if sym.startswith("C-") else PE)
            q = t.get("quotes") or {}
            mk = _f(t.get("mark_price"))
            if mk is not None:
                self.mark[key] = mk
            bid, ask = _f(q.get("best_bid")), _f(q.get("best_ask"))
            # An empty side is REMOVED rather than left stale — a fill simulated
            # against a bid that has since vanished is a fill that never happened.
            if bid is not None:
                self.bid[key] = bid
            else:
                self.bid.pop(key, None)
            if ask is not None:
                self.ask[key] = ask
            else:
                self.ask.pop(key, None)
            sp = _f(t.get("spot_price"))
            if sp:
                self.spot = sp
            n += 1
        if self.spot is not None and self.strikes:
            self.atm = atm_strike(self.spot, self.strikes)
        if n:
            self.updated = dt.datetime.now().strftime("%H:%M:%S")
        return n

    # ── what the strategy decides on ─────────────────────────────────────
    def chain(self) -> dict:
        """{(strike, type): mark} restricted to the band around ATM.

        Restricted deliberately: the far wings quote wide, stale and thin, and a
        selector scanning 40 levels outward would happily pick one. The band is
        the part of the chain we would actually be willing to trade.
        """
        if self.atm is None:
            return dict(self.mark)
        lo, hi = self._band()
        return {k: v for k, v in self.mark.items() if lo <= k[0] <= hi}

    def _band(self) -> tuple:
        """(low, high) strike bounds — `span` LISTED strikes each side of ATM,
        counted on the listed grid rather than by a fixed step, because the grid
        widens as you move away from the money."""
        i = self.strikes.index(self.atm) if self.atm in self.strikes else 0
        lo = self.strikes[max(0, i - self.span)]
        hi = self.strikes[min(len(self.strikes) - 1, i + self.span)]
        return lo, hi

    # ── what a paper order fills at ──────────────────────────────────────
    def fill_price(self, strike: float, opt_type: str, side: str) -> tuple:
        """(price, crossed_the_spread). A SELL hits the best bid, a BUY lifts the
        best ask. Falls back to the mark when that side of the book is empty, and
        says so, so a fill against a missing book is visible in the trade log
        rather than being quietly treated as real."""
        key = (float(strike), opt_type)
        book = self.bid if side == "SELL" else self.ask
        p = book.get(key)
        if p is not None:
            return p, True
        return self.mark.get(key), False

    def spread(self, strike: float, opt_type: str) -> float | None:
        key = (float(strike), opt_type)
        b, a = self.bid.get(key), self.ask.get(key)
        return None if (b is None or a is None) else round(a - b, 4)

    # ── health ───────────────────────────────────────────────────────────
    def is_ready(self) -> bool:
        """Enough of the chain has arrived to decide on. Selection needs a spot
        AND real marks on both sides — acting on a half-arrived chain would pick
        a strike off missing data."""
        if self.spot is None or self.atm is None:
            return False
        ce = sum(1 for (_, t) in self.mark if t == CE)
        pe = sum(1 for (_, t) in self.mark if t == PE)
        return ce >= 3 and pe >= 3

    def contract(self, strike: float, opt_type: str) -> dict | None:
        return self.by_key.get((float(strike), opt_type))

    def symbol_for(self, strike: float, opt_type: str) -> str | None:
        c = self.contract(strike, opt_type)
        return c["symbol"] if c else None

    def seconds_to_settlement(self, now: dt.datetime) -> int:
        return int((settlement_dt(self.expiry, self.settle_ist) - now).total_seconds())

    # ── for the snapshot / UI ────────────────────────────────────────────
    def rows(self, span: int = None) -> list:
        """One row per strike around ATM, nearest first."""
        if self.atm is None:
            return []
        span = span if span is not None else self.span
        i = self.strikes.index(self.atm) if self.atm in self.strikes else 0
        band = self.strikes[max(0, i - span): i + span + 1]
        out = []
        for s in band:
            out.append({
                "strike": s,
                "ce": self.mark.get((s, CE)), "pe": self.mark.get((s, PE)),
                "ce_bid": self.bid.get((s, CE)), "ce_ask": self.ask.get((s, CE)),
                "pe_bid": self.bid.get((s, PE)), "pe_ask": self.ask.get((s, PE)),
                "is_atm": s == self.atm,
            })
        return out
