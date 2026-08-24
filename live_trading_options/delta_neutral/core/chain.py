"""
core/chain.py — the live option chain, straight from Kite.
==========================================================

Holds one index's chain in memory and keeps it current from the Kite ticker.
Everything downstream — strike selection, leg marks, the terminal's chain table —
reads from here, so there is exactly one idea of "the current premium".

Kite identifies contracts by `instrument_token`, so that is the key the ticker
speaks in. This module owns the translation in both directions:

    instrument_token  <->  (strike, "CE"|"PE")  <->  tradingsymbol

Contracts are never hand-built. Every strike, token and lot size comes from
Kite's own instrument dump — the same dump the order layer resolves against, so
the thing we price is by construction the thing we trade.

Only the strikes the strategy could plausibly choose are streamed (ATM +/- span,
plus the index itself for spot). `tokens_to_add()` returns any that have come
into range as spot moves, so the engine can extend its subscription mid-session
instead of going blind after a big move.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as dt

from live.broker import kite_executor as kx
from core.selector import atm_strike, CE, PE

# where each index's options and its spot index live in Kite's world
OPT_EXCHANGE = {"NIFTY": "NFO", "SENSEX": "BFO"}
SPOT = {"NIFTY": ("NSE", "NIFTY 50"), "SENSEX": ("BSE", "SENSEX")}


def _instruments(kite, exchange: str):
    """Kite's contract list, via the order layer's cache so the big daily dump is
    fetched once per exchange per process rather than once per consumer."""
    return kx._instruments(kite, exchange)


def spot_token(kite, index: str) -> int | None:
    """instrument_token of the underlying index (NIFTY 50 = 256265, SENSEX = 265),
    looked up rather than hardcoded so it survives any renumbering."""
    exch, name = SPOT[index]
    for r in _instruments(kite, exch):
        if r.get("tradingsymbol") == name and r.get("segment") == "INDICES":
            return int(r["instrument_token"])
    return None


def option_rows(kite, index: str, expiry: dt.date) -> list:
    """Every CE/PE contract for one index+expiry."""
    return [r for r in _instruments(kite, OPT_EXCHANGE[index])
            if r.get("name") == index and r.get("instrument_type") in (CE, PE)
            and r.get("expiry") == expiry]


def expiries(kite, index: str) -> list:
    return sorted({r["expiry"] for r in _instruments(kite, OPT_EXCHANGE[index])
                   if r.get("name") == index and r.get("instrument_type") in (CE, PE)})


def nearest_expiry_and_dte(kite, index: str, today: dt.date = None):
    """(expiry, dte) for the front contract, from Kite's own expiry list.

    Replaces the Fyers symbol master entirely: same answer, one less data source,
    and no pandas in the runtime path."""
    today = today or dt.date.today()
    future = [e for e in expiries(kite, index) if e >= today]
    if not future:
        raise RuntimeError(f"no expiry on/after {today} for {index}")
    exp = future[0]
    return exp, (exp - today).days


def is_trade_day(kite, index: str, today: dt.date = None):
    """(tradeable, dte, expiry). This strategy trades DTE 0 and DTE 1 only."""
    exp, d = nearest_expiry_and_dte(kite, index, today)
    return (d in (0, 1)), d, exp


class LiveChain:
    def __init__(self, index: str, expiry: dt.date, interval: int, kite,
                 span: int = 15):
        self.index, self.expiry, self.interval = index, expiry, interval
        self.kite, self.span = kite, span
        self.spot: float | None = None
        self.atm: int | None = None
        self.updated: str | None = None

        self.ltp: dict[tuple, float] = {}         # (strike, type) -> premium
        self.by_token: dict[int, tuple] = {}      # instrument_token -> (strike, type)
        self.by_key: dict[tuple, dict] = {}       # (strike, type) -> contract
        self.strikes: list[int] = []
        self.spot_token: int | None = None
        self.lot_size: int | None = None
        self.subscribed: set[int] = set()

    # ── build from the instrument dump ───────────────────────────────────
    def load(self):
        """Resolve the whole expiry once: tokens, strikes, lot size, spot token."""
        self.spot_token = spot_token(self.kite, self.index)
        rows = option_rows(self.kite, self.index, self.expiry)
        if not rows:
            raise RuntimeError(f"no {self.index} contracts for {self.expiry} in Kite")
        for r in rows:
            key = (int(r["strike"]), r["instrument_type"])
            tok = int(r["instrument_token"])
            self.by_key[key] = {"token": tok, "tradingsymbol": r["tradingsymbol"],
                                "exchange": r.get("exchange") or OPT_EXCHANGE[self.index],
                                "lot_size": int(r["lot_size"])}
            self.by_token[tok] = key
        self.strikes = sorted({k[0] for k in self.by_key})
        self.lot_size = int(rows[0]["lot_size"])
        return self

    # ── what to stream ───────────────────────────────────────────────────
    def band(self, atm: int) -> list[int]:
        """Listed strikes within +/- span of ATM. Uses the LISTED strikes rather
        than stepping the interval, because the far wings are not always uniform."""
        lo, hi = atm - self.span * self.interval, atm + self.span * self.interval
        return [s for s in self.strikes if lo <= s <= hi]

    def tokens_to_add(self, spot: float = None) -> list[int]:
        """Tokens in range that are not yet subscribed, plus the spot token on the
        first call. Re-checked as spot drifts so the band follows the market."""
        spot = spot if spot is not None else self.spot
        out = []
        if self.spot_token and self.spot_token not in self.subscribed:
            out.append(self.spot_token)
        if spot is None:
            return out
        for strike in self.band(atm_strike(spot, self.interval)):
            for t in (CE, PE):
                c = self.by_key.get((strike, t))
                if c and c["token"] not in self.subscribed:
                    out.append(c["token"])
        return out

    def mark_subscribed(self, tokens: list[int]):
        self.subscribed.update(tokens)

    # ── contract lookup (never hand-built) ───────────────────────────────
    def contract(self, strike: int, opt_type: str) -> dict | None:
        return self.by_key.get((int(strike), opt_type))

    def symbol_for(self, strike: int, opt_type: str) -> str | None:
        """Kite tradingsymbol for a strike — the identifier a leg is stored under."""
        c = self.contract(strike, opt_type)
        return c["tradingsymbol"] if c else None

    # ── ticks ────────────────────────────────────────────────────────────
    def on_tick(self, token: int, ltp: float) -> bool:
        """Absorb one tick. True if it changed the chain or the spot."""
        token = int(token)
        if token == self.spot_token:
            self.spot = float(ltp)
            self.atm = atm_strike(self.spot, self.interval)
            self.updated = dt.datetime.now().strftime("%H:%M:%S")
            return True
        key = self.by_token.get(token)
        if key is None:
            return False
        self.ltp[key] = float(ltp)
        self.updated = dt.datetime.now().strftime("%H:%M:%S")
        return True

    def chain(self) -> dict:
        return dict(self.ltp)

    def is_ready(self) -> bool:
        """Enough of the chain has ticked to decide on. Selection needs a spot AND
        real premiums on both sides — acting on a half-arrived chain would pick a
        strike off missing data."""
        if self.spot is None or self.atm is None:
            return False
        ce = sum(1 for (_, t) in self.ltp if t == CE)
        pe = sum(1 for (_, t) in self.ltp if t == PE)
        return ce >= 3 and pe >= 3

    # ── for the terminal's chain table ───────────────────────────────────
    def rows(self, span: int = None) -> list[dict]:
        """One row per strike around ATM: {strike, ce, pe, is_atm}, nearest first."""
        if self.atm is None:
            return []
        span = span if span is not None else self.span
        lo, hi = self.atm - span * self.interval, self.atm + span * self.interval
        strikes = sorted(s for s in {k[0] for k in self.ltp} if lo <= s <= hi)
        return [{"strike": s, "ce": self.ltp.get((s, CE)), "pe": self.ltp.get((s, PE)),
                 "is_atm": s == self.atm} for s in strikes]
