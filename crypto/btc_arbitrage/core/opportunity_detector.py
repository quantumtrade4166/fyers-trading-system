"""
core/opportunity_detector.py — turns a stream of spreads into a few events.
==========================================================================

Only four things are ever logged, never every tick:

    STARTED   a direction's net-after-fees spread cleared the threshold and
              stayed there for OPPORTUNITY_MIN_MS (one-message blips from two
              sockets updating at different moments do not count)
    CHANGED   a running opportunity's net spread moved by OPPORTUNITY_CHANGE_USD
    ENDED     it dropped below the threshold, flipped direction, or the data
              stopped being trustworthy (stale / desync / disconnected)
    (connection events are raised by the monitor, not here)

Data that is not "OK" can never start or sustain an opportunity.
"""

from dataclasses import dataclass


@dataclass
class _Run:
    direction: str
    start_ms: float
    peak_net: float
    last_logged_net: float
    confirmed: bool = False


class OpportunityDetector:
    def __init__(self, min_ms: float, change_usd: float):
        self.min_ms = min_ms
        self.change_usd = change_usd
        self.run: _Run = None

    @property
    def active(self) -> bool:
        return bool(self.run and self.run.confirmed)

    def update(self, spread, quality: str, threshold: float, now_ms: float) -> list:
        """Feed one computed spread. Returns the events (usually none)."""
        candidate = None
        if quality == "OK" and spread.net >= threshold:
            candidate = spread.direction

        events = []
        r = self.run
        if r and candidate != r.direction:             # gone, flipped, or data went bad
            if r.confirmed:
                events.append(self._event("ENDED", spread, threshold, now_ms,
                                          duration_ms=now_ms - r.start_ms,
                                          peak_net=r.peak_net,
                                          reason="data " + quality if quality != "OK" else
                                                 ("flipped" if candidate else "below threshold")))
            self.run = r = None

        if candidate and r is None:
            self.run = r = _Run(candidate, now_ms, spread.net, spread.net)

        if r:
            r.peak_net = max(r.peak_net, spread.net)
            if not r.confirmed and now_ms - r.start_ms >= self.min_ms:
                r.confirmed = True
                r.last_logged_net = spread.net
                events.append(self._event("STARTED", spread, threshold, now_ms))
            elif r.confirmed and abs(spread.net - r.last_logged_net) >= self.change_usd:
                r.last_logged_net = spread.net
                events.append(self._event("CHANGED", spread, threshold, now_ms,
                                          duration_ms=now_ms - r.start_ms))
        return events

    @staticmethod
    def _event(kind, s, threshold, now_ms, **extra) -> dict:
        e = {"event": kind, "ts_ms": now_ms, "direction": s.direction,
             "d_bid": s.d_bid, "d_ask": s.d_ask, "b_bid": s.b_bid, "b_ask": s.b_ask,
             "gross": round(s.gross, 2), "fees": round(s.fees, 2), "net": round(s.net, 2),
             "spread_pct": round(s.gross_pct, 5), "net_pct": round(s.net_pct, 5),
             "threshold": round(threshold, 2)}
        e.update({k: (round(v, 2) if isinstance(v, float) else v) for k, v in extra.items()})
        return e
