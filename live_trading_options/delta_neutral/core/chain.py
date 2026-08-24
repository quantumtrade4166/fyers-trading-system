"""
core/chain.py — the live option chain.
======================================

Holds one index's chain in memory and keeps it current from the Fyers tick
stream. Everything downstream — strike selection, leg marks, the terminal's chain
table — reads from here, so there is exactly one idea of "the current premium" in
the system.

Shape:  {(strike:int, "CE"|"PE"): ltp:float}   plus a spot LTP

Two jobs:
  1. decide WHICH contracts to stream (ATM +/- span, both sides, plus the index
     itself for spot) and resolve their Fyers symbols from the symbol master
  2. absorb ticks into the chain

The streamed band is re-checked as spot moves: gold-standard would be to stream
every strike, but a weekly chain is hundreds of contracts and the socket only
needs the strikes the strategy could actually choose. `symbols_to_add()` returns
any new strikes that have come into range so the engine can subscribe to them
mid-session instead of going blind after a big move.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as dt

from core.shared import symbol_master
from core.selector import atm_strike, CE, PE


class LiveChain:
    def __init__(self, index: str, expiry: dt.date, interval: int,
                 index_symbol: str, span: int = 15):
        self.index, self.expiry, self.interval = index, expiry, interval
        self.index_symbol = index_symbol
        self.span = span
        self.spot: float | None = None
        self.atm: int | None = None
        self.ltp: dict[tuple, float] = {}          # (strike, type) -> premium
        self.sym_to_key: dict[str, tuple] = {}     # fyers symbol -> (strike, type)
        self.key_to_sym: dict[tuple, str] = {}
        self.subscribed: set[str] = set()
        self._all_strikes: list[int] = []
        self.updated: str | None = None

    # ── symbol resolution ────────────────────────────────────────────────
    def load_strikes(self):
        """Every strike the exchange lists for this expiry (from the symbol
        master, not guessed by stepping the interval — listed strikes have gaps
        and the far wings are not always uniform)."""
        self._all_strikes = symbol_master.available_strikes(self.index, self.expiry, CE)
        return self._all_strikes

    def band(self, atm: int) -> list[int]:
        """Listed strikes within +/- span of ATM."""
        if not self._all_strikes:
            self.load_strikes()
        lo, hi = atm - self.span * self.interval, atm + self.span * self.interval
        return [s for s in self._all_strikes if lo <= s <= hi]

    def _register(self, strike: int, opt_type: str) -> str | None:
        key = (int(strike), opt_type)
        if key in self.key_to_sym:
            return self.key_to_sym[key]
        sym = symbol_master.find_symbol(self.index, self.expiry, strike, opt_type)
        if not sym:
            return None
        self.key_to_sym[key] = sym
        self.sym_to_key[sym] = key
        return sym

    def symbol_for(self, strike: int, opt_type: str) -> str | None:
        """The Fyers symbol for a strike — the lookup the controller uses when it
        builds a leg. Registers the contract on first use so a strike chosen just
        outside the streamed band still resolves."""
        return self._register(strike, opt_type)

    def symbols_to_add(self, spot: float = None) -> list[str]:
        """Contracts in range that are not yet subscribed (plus the index symbol
        on the first call). Called at start and periodically as spot drifts."""
        spot = spot if spot is not None else self.spot
        out = []
        if self.index_symbol not in self.subscribed:
            out.append(self.index_symbol)
        if spot is None:
            return out
        atm = atm_strike(spot, self.interval)
        for strike in self.band(atm):
            for t in (CE, PE):
                sym = self._register(strike, t)
                if sym and sym not in self.subscribed:
                    out.append(sym)
        return out

    def mark_subscribed(self, symbols: list[str]):
        self.subscribed.update(symbols)

    # ── ticks ────────────────────────────────────────────────────────────
    def on_tick(self, symbol: str, ltp: float):
        """Absorb one tick. Returns True if it changed the chain or the spot."""
        if symbol == self.index_symbol:
            self.spot = float(ltp)
            self.atm = atm_strike(self.spot, self.interval)
            self.updated = dt.datetime.now().strftime("%H:%M:%S")
            return True
        key = self.sym_to_key.get(symbol)
        if key is None:
            return False
        self.ltp[key] = float(ltp)
        self.updated = dt.datetime.now().strftime("%H:%M:%S")
        return True

    def chain(self) -> dict:
        return dict(self.ltp)

    def is_ready(self) -> bool:
        """Enough of the chain has ticked to make a decision on. Selection needs a
        spot AND real premiums on both sides — acting on a half-arrived chain would
        pick a strike off stale or missing data."""
        if self.spot is None or self.atm is None:
            return False
        ce = sum(1 for (s, t) in self.ltp if t == CE)
        pe = sum(1 for (s, t) in self.ltp if t == PE)
        return ce >= 3 and pe >= 3

    # ── for the terminal's chain table ───────────────────────────────────
    def rows(self, span: int = None) -> list[dict]:
        """One row per strike around ATM: {strike, ce, pe, is_atm}, nearest first.
        This is what the UI renders, so it is built here rather than in JS."""
        if self.atm is None:
            return []
        span = span if span is not None else self.span
        lo, hi = self.atm - span * self.interval, self.atm + span * self.interval
        strikes = sorted(s for s in {k[0] for k in self.ltp} if lo <= s <= hi)
        return [{"strike": s, "ce": self.ltp.get((s, CE)), "pe": self.ltp.get((s, PE)),
                 "is_atm": s == self.atm} for s in strikes]
