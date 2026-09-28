"""
JARVIS Agent — DualMom
=======================
Dual Momentum monitoring agent.

Responsibilities:
  - Monitor DualMom portfolio health
  - Check last trading signal
  - Alert on issues (missing data, connection failures)
  - Provide portfolio summary

Trigger events (Phase 2+):
  - Time-based (every N minutes during market hours)
  - Signal-based (when a new signal is generated)
  - Error-based (on connection failure)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ..logging_config import get_logger
from .base_agent import BaseAgent

log = get_logger("agent_dualmom")


class DualMomAgent(BaseAgent):
    """Dual Momentum portfolio monitoring agent."""

    name = "dualmom_agent"
    description = "Monitors DualMom portfolio health and signals"
    permission_level = 1

    async def _execute(self, command: str, **kwargs: Any) -> dict:
        cmd = command.lower()

        if "status" in cmd or "health" in cmd:
            return await self._check_health()
        elif "portfolio" in cmd:
            return await self._get_portfolio_summary()
        elif "signal" in cmd:
            return await self._get_last_signal()
        elif "restart" in cmd:
            return {"status": "manual_action", "message": "Use dashboard to restart engine"}
        else:
            return {
                "status": "ok",
                "agent": self.name,
                "message": "DualMom agent ready. Use: status, portfolio, signal",
            }

    async def _check_health(self) -> dict:
        """Check DualMom engine health."""
        return {
            "status": "running",
            "agent": self.name,
            "engine_status": "unknown",  # Phase 2: query dashboard API
            "last_signal_time": "unknown",
            "portfolio_size": "unknown",
        }

    async def _get_portfolio_summary(self) -> dict:
        """Get current portfolio summary."""
        return {
            "status": "ok",
            "agent": self.name,
            "message": "Portfolio summary requires dashboard API — Phase 2",
        }

    async def _get_last_signal(self) -> dict:
        """Get the last trading signal."""
        return {
            "status": "ok",
            "agent": self.name,
            "message": "Signal query requires dashboard API — Phase 2",
        }
