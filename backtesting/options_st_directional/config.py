"""Backtest configuration. Defaults reproduce the baseline strategy exactly."""
from dataclasses import dataclass, field, asdict


@dataclass
class Config:
    # period
    start: str = "2026-08-10"
    end: str = "2026-10-09"

    # capital / size
    capital: float = 1_000_000.0
    lots: int = 1
    margin_per_lot: float = 115_000.0      # used only for capital-utilisation stats

    # indicators (spot and option use the same settings)
    st_period: int = 10
    st_mult: float = 3.0
    timeframe_min: int = 5

    # session
    first_candle: str = "09:15"            # 09:15-09:20 candle -> initial bias at 09:20
    square_off: str = "15:15"              # pure intraday: flat at 15:15, no entry fill at/after it

    # instrument selection
    strike_step: int = 50
    # Skip the front expiry when this many (or fewer) trading sessions remain after
    # today, expiry day included. 2 -> Fri/Mon/Tue of a Tuesday-expiry week go next week.
    switch_sessions_left: int = 2
    option_warmup_days: int = 1            # previous sessions of the SAME contract for option ST warm-up

    # execution model
    # A signal confirmed at a 5-min candle close T fills at the OPEN of the first
    # 1-minute bar at/after T, searched up to fill_search_min minutes.
    fill_search_min: int = 5
    slippage_points: float = 0.0           # per order (entry and exit each), charged as a cost

    tag: str = "BASELINE"

    def to_dict(self):
        return asdict(self)


# NIFTY lot size by trade date. Only sizes verified for the periods this project
# trades are listed; an earlier date raises instead of guessing.
LOT_SIZE_SCHEDULE = [
    ("2024-11-20", 75),
    ("2025-12-30", 65),
]


def lot_size(date_str: str) -> int:
    size = None
    for eff, n in LOT_SIZE_SCHEDULE:
        if date_str >= eff:
            size = n
    if size is None:
        raise ValueError(f"NIFTY lot size not configured for {date_str}")
    return size
