"""
JARVIS Agent — Fyers
====================
Fyers data ingestion agent.

Responsibilities:
  - Check data freshness (latest candle timestamps)
  - Trigger data updates
  - Report download errors
  - Validate data integrity
"""

from __future__ import annotations

from typing import Any, Optional

from ..logging_config import get_logger
from .base_agent import BaseAgent

log = get_logger("agent_fyers")


class FyersAgent(BaseAgent):
    """Fyers data pipeline monitoring agent."""

    name = "fyers_agent"
    description = "Monitors Fyers data pipeline health and freshness"
    permission_level = 1

    async def _execute(self, command: str, **kwargs: Any) -> dict:
        cmd = command.lower()

        if "status" in cmd or "freshness" in cmd or "data" in cmd:
            return await self._check_freshness()
        elif "update" in cmd or "refresh" in cmd:
            return await self._trigger_update()
        elif "manifest" in cmd:
            return await self._get_manifest()
        else:
            return {
                "status": "ok",
                "agent": self.name,
                "message": "Fyers agent ready. Use: status, update, manifest",
            }

    async def _check_freshness(self) -> dict:
        """Check data freshness from the manifest."""
        try:
            from jarvis.integrations.fyers_loader import get_data_status
            status = await get_data_status()
            return {
                "status": "ok",
                "agent": self.name,
                "data_status": status,
            }
        except ImportError:
            return {
                "status": "partial",
                "agent": self.name,
                "message": "Fyers loader not yet implemented — Phase 1+",
                "data_status": {"error": "loader_not_available"},
            }

    async def _trigger_update(self) -> dict:
        """Trigger a data update."""
        return {
            "status": "manual_action",
            "agent": self.name,
            "message": "Use 'python run_pipeline.py --mode update' to trigger update",
        }

    async def _get_manifest(self) -> dict:
        """Get data manifest info."""
        try:
            from tracker.manifest import get_manifest
            manifest = get_manifest()
            return {
                "status": "ok",
                "agent": self.name,
                "manifest": manifest,
            }
        except ImportError:
            return {"status": "error", "message": "Manifest not available"}
