"""
cepe_imbalance/market.py — latest quote per contract + a live chain view per underlying.
=========================================================================================

Reference price (what ATM is measured from):
  NIFTY / SENSEX  -> index SPOT (Kotak index packet)
  MCX             -> the FUTURE the chosen option expiry is written on
ATM = listed strike nearest the reference price.
"""
import threading
import time
from dataclasses import dataclass

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance.feed import Tick


@dataclass
class Quote:
    ltp: float = 0.0
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: int | None = None
    oi: int | None = None
    exch_ts: int = 0
    recv_ts: float = 0.0

    def mid(self) -> float:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.ltp


class Market:
    def __init__(self, universe):
        self.u = universe
        self.quotes: dict[str, Quote] = {}
        self.lock = threading.RLock()

    # ── ticks ───────────────────────────────────────────────────────────────
    def apply(self, ticks: list) -> list:
        """Store ticks; returns the keys that changed."""
        keys = []
        with self.lock:
            for t in ticks:
                q = self.quotes.get(t.key)
                if q is None:
                    q = self.quotes[t.key] = Quote()
                q.ltp = t.ltp or q.ltp
                if t.bid is not None or t.ask is not None:
                    q.bid, q.ask, q.bid_qty, q.ask_qty = t.bid, t.ask, t.bid_qty, t.ask_qty
                if t.volume is not None:
                    q.volume = t.volume
                if t.oi is not None:
                    q.oi = t.oi
                q.exch_ts, q.recv_ts = t.exch_ts, t.recv_ts
                keys.append(t.key)
        return keys

    def quote(self, key: str) -> Quote | None:
        return self.quotes.get(key)

    def ltp(self, key: str) -> float:
        q = self.quotes.get(key)
        return q.ltp if q else 0.0

    # ── chain helpers ───────────────────────────────────────────────────────
    def expiry(self, name: str, n: int = 0) -> str | None:
        e = self.u.underlyings[name].expiries()
        return e[n] if len(e) > n else None

    def ref_key(self, name: str, expiry: str | None = None) -> str | None:
        und = self.u.underlyings[name]
        if und.spot_key:
            return und.spot_key
        expiry = expiry or self.expiry(name)
        f = und.future_for(expiry) if expiry else None
        return f.key if f else None

    def ref_price(self, name: str, expiry: str | None = None) -> float:
        k = self.ref_key(name, expiry)
        return self.ltp(k) if k else 0.0

    def atm(self, name: str, expiry: str | None = None) -> float | None:
        expiry = expiry or self.expiry(name)
        px = self.ref_price(name, expiry)
        strikes = self.u.underlyings[name].strikes(expiry) if expiry else []
        if px <= 0 or not strikes:
            return None
        return min(strikes, key=lambda s: (abs(s - px), s))

    def synthetic(self, name: str, expiry: str | None = None, strikes: int = 3) -> float | None:
        """Put-call parity price from the strikes nearest ATM: strike + CE mid - PE mid (averaged).
        Model-free; immune to NSE's closing-auction index values after 15:15 (checked 2-Nov-2026:
        index 23,440.8 vs options-implied 23,352)."""
        expiry = expiry or self.expiry(name)
        if not expiry:
            return None
        vals = []
        with self.lock:
            for s in self.window(name, expiry, strikes // 2):
                ce, pe = self.contract(name, expiry, s, "CE"), self.contract(name, expiry, s, "PE")
                qc = self.quotes.get(ce.key) if ce else None
                qp = self.quotes.get(pe.key) if pe else None
                if qc and qp and qc.mid() > 0 and qp.mid() > 0:
                    vals.append(s + qc.mid() - qp.mid())
        return round(sum(vals) / len(vals), 2) if vals else None

    def contract(self, name: str, expiry: str, strike: float, kind: str):
        row = self.u.underlyings[name].chains.get(expiry, {}).get(float(strike))
        return row.get(kind) if row else None

    def window(self, name: str, expiry: str, each_side: int = C.STRIKES_EACH_SIDE, center: float | None = None) -> list:
        """Strikes around ATM (or `center`) for one expiry."""
        strikes = self.u.underlyings[name].strikes(expiry)
        if not strikes:
            return []
        c = center if center is not None else self.atm(name, expiry)
        if c is None:
            c = strikes[len(strikes) // 2]
        i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - c))
        return strikes[max(0, i - each_side): i + each_side + 1]

    def chain_rows(self, name: str, expiry: str, each_side: int = 10) -> list:
        out = []
        with self.lock:
            atm = self.atm(name, expiry)
            for s in self.window(name, expiry, each_side):
                row = {"strike": s, "atm": s == atm}
                for kind in ("CE", "PE"):
                    c = self.contract(name, expiry, s, kind)
                    q = self.quotes.get(c.key) if c else None
                    row[kind] = {"key": c.key if c else None, "symbol": c.symbol if c else None,
                                 "ltp": q.ltp if q else None, "bid": q.bid if q else None,
                                 "ask": q.ask if q else None, "oi": q.oi if q else None,
                                 "age": round(time.time() - q.recv_ts, 1) if q and q.recv_ts else None}
                out.append(row)
        return out
