"""
core/market_data.py — the latest valid top of book from each exchange.
=====================================================================

Each exchange client writes into its own slot; the spread engine reads both.
Nothing here compares anything — it only remembers, per exchange:

    bid / ask / sizes (sizes always in BTC, whatever the exchange's unit)
    exchange_ts_ms   the exchange's own timestamp for the quote
    recv_ms          OUR wall clock when the message arrived
    recv_mono        OUR monotonic clock when it arrived (ages are taken from
                     this — it cannot jump when Windows resyncs the clock)

and the connection state the dashboard shows (CONNECTED / DISCONNECTED).

A quote with a missing or crossed bid/ask is rejected and the previous valid
quote is kept; its age keeps growing, so it goes STALE on its own instead of
being silently replaced by garbage.
"""

import time
from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class Quote:
    bid: float
    bid_qty: float          # BTC
    ask: float
    ask_qty: float          # BTC
    exchange_ts_ms: float
    recv_ms: float
    recv_mono: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    def age_ms(self, now_mono: float = None) -> float:
        return ((now_mono if now_mono is not None else time.monotonic()) - self.recv_mono) * 1000


def valid_quote(bid, ask) -> bool:
    """Both sides present, positive, and not crossed."""
    return (bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid)


@dataclass
class Feed:
    name: str
    quote: Quote = None
    connected: bool = False
    since: float = 0.0              # wall time of the last connect/disconnect
    reconnects: int = 0
    messages: int = 0
    rejected: int = 0               # malformed / missing / crossed quotes
    last_error: str = ""
    funding_rate: float = None      # per funding interval, fraction
    funding_interval_h: float = None
    next_funding_ms: float = None
    extra: dict = field(default_factory=dict)

    def status(self, now_mono: float) -> dict:
        d = asdict(self)
        d.pop("quote")
        q = self.quote
        d["age_ms"] = round(q.age_ms(now_mono)) if q else None
        return d


@dataclass(frozen=True)
class Depth:
    bids: list              # [(price, qty_btc), ...] best first
    asks: list
    exchange_ts_ms: float
    recv_mono: float

    def age_ms(self, now_mono: float = None) -> float:
        return ((now_mono if now_mono is not None else time.monotonic()) - self.recv_mono) * 1000


class MarketData:
    def __init__(self, on_update=None):
        self.delta = Feed("delta")
        self.binance = Feed("binance")
        self.depth = {"delta": None, "binance": None}   # latest order book per exchange
        self.usdc_usdt = None       # USDC price in USDT (USDC ~ USD)
        self.clock_offset_ms = 0.0  # true_time - local_time, from the clock sync
        self._on_update = on_update

    def put_depth(self, name: str, bids: list, asks: list, exchange_ts_ms):
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            self.feed(name).rejected += 1
            return False
        self.depth[name] = Depth(bids, asks, exchange_ts_ms or 0.0, time.monotonic())
        return True

    def clear_depth(self, name: str):
        self.depth[name] = None

    def feed(self, name: str) -> Feed:
        return self.delta if name == "delta" else self.binance

    def put(self, name: str, bid, bid_qty, ask, ask_qty, exchange_ts_ms) -> bool:
        f = self.feed(name)
        f.messages += 1
        if not valid_quote(bid, ask):
            f.rejected += 1
            return False
        prev = f.quote
        f.quote = Quote(bid, bid_qty or 0.0, ask, ask_qty or 0.0, exchange_ts_ms or 0.0,
                        time.time() * 1000, time.monotonic())
        # Binance pushes ~500 updates/s, most of them size-only. The spread only
        # depends on prices, so recompute only when a price moved (the fresh
        # receive time and sizes are stored either way; the 250 ms publisher
        # re-evaluates regardless, so staleness is still caught).
        if prev and prev.bid == bid and prev.ask == ask:
            return True
        if self._on_update:
            self._on_update(name)
        return True

    def set_connected(self, name: str, up: bool, error: str = ""):
        f = self.feed(name)
        if up and not f.connected and f.since:
            f.reconnects += 1
        f.connected = up
        f.since = time.time()
        if error:
            f.last_error = error[:300]

    def latency_ms(self, name: str):
        """Exchange stamp -> our receipt, corrected for our clock's offset.
        Only meaningful after the first clock sync."""
        q = self.feed(name).quote
        if not q or not q.exchange_ts_ms:
            return None
        return round(q.recv_ms + self.clock_offset_ms - q.exchange_ts_ms, 1)
