"""
strangle_system/signals.py
===========================
Typed signal objects passed between layers. Each layer outputs one small,
typed dataclass; Layer 5 consumes them. Defining all of them upfront fixes the
inter-layer contracts even though only Layer 1 is populated in this phase.

Engineering rule (§8): signal objects are @dataclass; point-in-time correct
(every field for date T uses only data available at/before T).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional


class DataQuality(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"      # usable but some inputs missing/estimated
    INSUFFICIENT = "INSUFFICIENT"  # not enough history (e.g. IV rank) — fail safe
    MISSING = "MISSING"        # required input absent → must default to no-trade


class TrendRegime(str, Enum):
    RANGE = "RANGE"
    WEAK_TREND = "WEAK_TREND"
    STRONG_TREND = "STRONG_TREND"


class TermState(str, Enum):
    CONTANGO = "CONTANGO"
    FLAT = "FLAT"
    BACKWARDATION = "BACKWARDATION"


class GexRegime(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"


@dataclass
class VolatilitySignal:
    """Layer 1 output — the core volatility edge."""
    underlying: str
    asof: str                       # YYYY-MM-DD (point-in-time date)
    rv_5: Optional[float] = None    # annualized realized vol, 5d
    rv_10: Optional[float] = None
    rv_20: Optional[float] = None
    rv_forecast: Optional[float] = None   # forecast over holding horizon
    rv_forecast_method: Optional[str] = None  # "ewma" | "garch"
    atm_iv: Optional[float] = None        # annualized, same horizon
    vrp: Optional[float] = None           # atm_iv − rv_forecast
    iv_rank: Optional[float] = None       # 0..100 over trailing window
    iv_percentile: Optional[float] = None
    horizon_days: Optional[int] = None    # days to the sold expiry
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    data_quality: DataQuality = DataQuality.OK
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["data_quality"] = self.data_quality.value
        return d


@dataclass
class GuardrailSignal:
    """Layer 2 output — hard vetoes & regime (built in Phase 2)."""
    underlying: str
    asof: str
    event_veto: bool = False
    event_reason: str = ""
    is_expiry_day: bool = False
    days_to_expiry: Optional[int] = None
    trend_regime: TrendRegime = TrendRegime.RANGE
    adx: Optional[float] = None
    data_quality: DataQuality = DataQuality.OK


@dataclass
class TermStructureSignal:
    """Layer 3 output (built in Phase 3)."""
    underlying: str
    asof: str
    term_structure_slope: Optional[float] = None
    term_state: TermState = TermState.FLAT
    skew_25d: Optional[float] = None
    skew_state: str = ""
    data_quality: DataQuality = DataQuality.OK


@dataclass
class GexSignal:
    """Layer 4 output (built + GATED in Phase 4 — validated must be True to use)."""
    underlying: str
    asof: str
    net_gex: Optional[float] = None
    gamma_flip: Optional[float] = None
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    gex_regime: Optional[GexRegime] = None
    validated: bool = False        # GATE: cannot influence L5 until backtest passes
    data_quality: DataQuality = DataQuality.OK


@dataclass
class Decision:
    """Layer 5 output — the morning flag (built in Phase 5)."""
    date: str
    underlying: str
    trade: bool
    verdict_reason: str
    score: Optional[float] = None
    regime: dict = field(default_factory=dict)
    suggested: dict = field(default_factory=dict)
    size: dict = field(default_factory=dict)
    guardrails: dict = field(default_factory=dict)
