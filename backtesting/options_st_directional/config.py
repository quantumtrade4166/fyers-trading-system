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

    # OPTIONAL extension (off in the baseline): once the premium has fallen
    # `breakeven_after_points` below the sell price, a stop sits at the sell price.
    # Checked on 1-minute bars; a minute that touches both levels counts as NOT armed
    # yet (worst case); fill = the sell price, or the minute's open if it gapped past.
    breakeven_after_points: float | None = None

    # OPTIONAL day filter (off in the baseline): skip the whole day when the CPR
    # built from the previous session is wider than this many points (|TC - BC|).
    cpr_max_width: float | None = None

    # OPTIONAL entry filter (off in the baseline): NIFTY 5-min RSI at the signal
    # candle must agree -- sell PE only if RSI > 50, sell CE only if RSI < 50.
    # A blocked candle is simply not entered; the setup keeps its reference low.
    rsi_filter: bool = False
    rsi_period: int = 14

    tag: str = "BASELINE"

    def to_dict(self):
        return asdict(self)


# NIFTY lot size by CONTRACT EXPIRY (a revision applies to newly listed contracts;
# existing ones keep their size until they expire). From the NSE revisions:
#   50 -> 25: first weekly 02-May-2024 (effective 26-Apr-2024)
#   25 -> 75: last weekly with 25 = 19-Dec-2024, monthly 26-Dec-2024; first 75 = 02-Jan-2025
#   75 -> 65: last weekly with 75 = 23-Dec-2025, monthly 30-Dec-2025; first 65 = 06-Jan-2026
# Expiries before 2024 are not configured and raise instead of guessing.
LOT_SIZE_BY_EXPIRY = [
    ("2024-01-01", 50),
    ("2024-04-26", 25),
    ("2024-12-27", 75),
    ("2025-12-31", 65),
]


def lot_size(expiry) -> int:
    e = str(expiry)[:10]
    size = None
    for eff, n in LOT_SIZE_BY_EXPIRY:
        if e >= eff:
            size = n
    if size is None:
        raise ValueError(f"NIFTY lot size not configured for expiry {e}")
    return size
