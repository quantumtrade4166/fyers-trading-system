"""
core/fees.py — what crossing both books costs, per 1 BTC.
=========================================================

Taker on both legs (the monitor assumes we cross the spread on both exchanges):

    Delta leg    price_D x DELTA_TAKER_FEE x (1 + DELTA_FEE_GST)
    Binance leg  price_B x BINANCE_TAKER_FEE

    entry      = Delta leg + Binance leg              (2 fills)
    round_trip = 2 x entry                            (4 fills: open + close)

Round trip prices the exit at today's prices — near enough, because the exit
happens within a few dollars of the entry on a ~$84k contract.

At the defaults (0.05% + 18% GST, 0.05%) a round trip is ~0.218% of price —
about $183 per BTC at $84k. That is the bar the spread has to clear.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeModel:
    delta_taker: float
    delta_gst: float
    binance_taker: float
    mode: str = "round_trip"        # "round_trip" | "entry"

    def __post_init__(self):
        if self.mode not in ("round_trip", "entry"):
            raise ValueError(f"FEE_MODE must be 'round_trip' or 'entry', got {self.mode!r}")

    @property
    def delta_rate(self) -> float:
        return self.delta_taker * (1 + self.delta_gst)

    @property
    def legs(self) -> int:
        return 2 if self.mode == "round_trip" else 1

    def cost(self, delta_price: float, binance_price: float) -> float:
        """USD per 1 BTC for the configured mode, given the prices each leg trades at."""
        one = delta_price * self.delta_rate + binance_price * self.binance_taker
        return one * self.legs

    def cost_pct(self) -> float:
        """The same cost as a percent of price (prices ~equal on both sides)."""
        return (self.delta_rate + self.binance_taker) * self.legs * 100

    @classmethod
    def from_config(cls, cfg):
        return cls(cfg.DELTA_TAKER_FEE, cfg.DELTA_FEE_GST, cfg.BINANCE_TAKER_FEE, cfg.FEE_MODE)
