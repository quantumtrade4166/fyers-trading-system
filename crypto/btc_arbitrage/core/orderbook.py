"""
core/orderbook.py — local order books for the depth-priced (1 BTC) spread.
=========================================================================

DELTA — `l2_updates` channel (verified live 2026-10-04):
    first message  {"action":"snapshot","bids":[["84808.0","4718"],...],"asks":[...],
                    "sequence_no":10313995,"cs":...,"timestamp":<us>}   (~2,600 bid levels)
    then           {"action":"update","bids":[["84616.5","0"],...],"asks":[...],
                    "sequence_no":10313996, ...}                         every ~0.2-0.5 s
    size "0" removes a level. Sizes are CONTRACTS (x 0.001 BTC).
    sequence_no must step by exactly 1 — any gap means we missed an update,
    so the book is thrown away and the socket rebuilt (ResyncRequired).

BINANCE — `<symbol>@depth20@100ms` partial book: the top 20 levels, complete,
every 100 ms. Nothing to maintain — each message replaces the last.

Both end up as the same shape for core/depth.walk():
    bids [(price, qty_btc), ...] best first,  asks [(price, qty_btc), ...] best first
"""

import heapq

TOP_LEVELS = 50          # enough for 1 BTC many times over on both exchanges


class ResyncRequired(Exception):
    """The local book can no longer be trusted — rebuild the socket."""


class DeltaBook:
    def __init__(self, contract_value_btc: float):
        self.cv = contract_value_btc
        self.bids: dict = {}
        self.asks: dict = {}
        self.seq = None
        self.ts_ms = None
        self.top_bids: list = []
        self.top_asks: list = []

    def reset(self):
        self.bids.clear(); self.asks.clear()
        self.seq = None; self.top_bids = []; self.top_asks = []

    def apply(self, msg: dict) -> bool:
        """Returns True when the book changed and is valid."""
        action = msg.get("action")
        seq = msg.get("sequence_no")
        if action == "snapshot":
            self.bids = {float(p): float(s) for p, s in msg.get("bids", []) if float(s) > 0}
            self.asks = {float(p): float(s) for p, s in msg.get("asks", []) if float(s) > 0}
        elif action == "update":
            if self.seq is None:
                return False                     # updates before the snapshot: ignore
            if seq is not None and seq != self.seq + 1:
                self.reset()
                raise ResyncRequired(f"delta l2 sequence gap {self.seq} -> {seq}")
            for side, book in (("bids", self.bids), ("asks", self.asks)):
                for p, s in msg.get(side, []):
                    p, s = float(p), float(s)
                    if s > 0:
                        book[p] = s
                    else:
                        book.pop(p, None)
        else:
            return False
        self.seq = seq
        ts = msg.get("timestamp")
        self.ts_ms = ts / 1000 if ts else None
        self.top_bids = [(p, self.bids[p] * self.cv) for p in heapq.nlargest(TOP_LEVELS, self.bids)]
        self.top_asks = [(p, self.asks[p] * self.cv) for p in heapq.nsmallest(TOP_LEVELS, self.asks)]
        if self.top_bids and self.top_asks and self.top_bids[0][0] >= self.top_asks[0][0]:
            self.reset()
            raise ResyncRequired("delta l2 book crossed — rebuilding")
        return bool(self.top_bids and self.top_asks)
