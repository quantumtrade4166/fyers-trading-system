"""
base.py
Common data shapes + adapter interface for the multi-broker terminal.

Every broker (Fyers, Zerodha, Jainam) implements BrokerAdapter and returns a
BrokerSnapshot in the SAME unified format so the frontend never has to know
which broker a position came from.

This terminal is READ-ONLY. Adapters never place, modify, or cancel orders.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Position:
    broker:     str               # "fyers" | "zerodha" | "jainam"
    symbol:     str               # display symbol, e.g. "RELIANCE"
    exchange:   str = ""          # NSE / BSE / NFO ...
    product:    str = ""          # CNC / MIS / NRML / INTRADAY ...
    side:       str = "FLAT"      # LONG | SHORT | FLAT
    qty:        int = 0           # signed net qty (+long / -short)
    avg_price:  float = 0.0
    ltp:        float = 0.0
    pnl:        float = 0.0        # unrealised / M2M as reported by the broker
    realised:   float = 0.0       # realised P&L for the day (if available)

    def as_dict(self) -> dict:
        d = asdict(self)
        # round floats for clean JSON
        for k in ("avg_price", "ltp", "pnl", "realised"):
            d[k] = round(float(d[k]), 2)
        return d


@dataclass
class BrokerSnapshot:
    broker:    str
    status:    str = "ok"          # ok | not_configured | error
    message:   str = ""            # error / status detail for the UI
    positions: list = field(default_factory=list)   # list[Position]
    margin_used:      float = 0.0  # margin/funds utilised on this account
    margin_available: float = 0.0  # free margin still available

    @property
    def margin_total(self) -> float:
        return round(self.margin_used + self.margin_available, 2)

    @property
    def total_pnl(self) -> float:
        return round(sum(p.pnl for p in self.positions), 2)

    @property
    def realised_pnl(self) -> float:
        return round(sum(p.realised for p in self.positions), 2)

    @property
    def open_count(self) -> int:
        return sum(1 for p in self.positions if p.qty != 0)

    def as_dict(self) -> dict:
        return {
            "broker":           self.broker,
            "status":           self.status,
            "message":          self.message,
            "total_pnl":        self.total_pnl,
            "realised_pnl":     self.realised_pnl,
            "open_count":       self.open_count,
            "margin_used":      round(float(self.margin_used), 2),
            "margin_available": round(float(self.margin_available), 2),
            "margin_total":     self.margin_total,
            "positions":        [p.as_dict() for p in self.positions],
        }


class BrokerAdapter:
    """Subclass and implement is_configured() + fetch_snapshot()."""
    name = "base"

    def is_configured(self) -> bool:
        raise NotImplementedError

    def fetch_snapshot(self) -> BrokerSnapshot:
        """Return a BrokerSnapshot. Must never raise — wrap errors into status='error'."""
        raise NotImplementedError

    # helper used by every adapter so one broker failing never breaks the terminal
    def _safe(self, fn) -> BrokerSnapshot:
        if not self.is_configured():
            return BrokerSnapshot(self.name, status="not_configured",
                                  message="No API credentials in .env")
        try:
            return fn()
        except Exception as e:
            return BrokerSnapshot(self.name, status="error", message=str(e)[:200])
