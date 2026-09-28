"""
JARVIS Agent Base Class
========================
Abstract base for all JARVIS agents.

Pattern:
  1. Receive a command
  2. Check permissions
  3. Check kill switch
  4. Execute action
  5. Record in ledger + timeline
  6. Return result

Specialized Agents:
  - DualMomAgent: market monitoring for DualMom signals
  - FyersAgent: data ingestion and health checks
  - MonitoringAgent: VPS and dashboard health
"""

from __future__ import annotations

import inspect
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from ..logging_config import get_logger
from ..memory.command_ledger import get_ledger
from ..memory.timeline import Timeline, utc_now
from ..security.audit import AuditTrail
from ..security.kill_switch import get_kill_switch
from ..security.permissions import get_permissions

log = get_logger("agents")


class BaseAgent(ABC):
    """Abstract base class for all JARVIS agents."""

    name: str = "base_agent"
    description: str = "Base agent — should be subclassed"
    permission_level: int = 0  # L0 by default

    def __init__(
        self,
        db: Any = None,
        *,
        audit: Optional[AuditTrail] = None,
        ledger: Any = None,
        timeline: Optional[Timeline] = None,
        permissions: Any = None,
        kill_switch: Any = None,
    ) -> None:
        self._db = db
        self._audit = audit
        self._ledger = ledger or get_ledger()
        self._timeline = timeline or Timeline(db)
        self._perms = permissions or get_permissions()
        self._kill = kill_switch or get_kill_switch()

    async def run(self, command: str, **kwargs: Any) -> dict:
        """
        Main entry point: execute a command with full safety checks.

        Args:
            command: The command string.
            **kwargs: Additional parameters.

        Returns:
            Result dict with status and data.
        """
        start = time.time()
        command_id = None

        # Record command
        try:
            record = await self._ledger.record(
                command,
                detected_intent=self._detect_intent(command),
                target_agent=self.name,
            )
            command_id = record.get("command_id")
        except Exception as e:
            log.warning("ledger_record_failed", error=str(e))

        # Kill switch
        try:
            self._kill.check(command)
        except Exception as e:
            await self._audit.record(
                actor=self.name,
                action=command,
                command_id=command_id,
                result="blocked",
                error_info=str(e),
            )
            return {"status": "blocked", "error": str(e)}

        # Execute
        try:
            result = await self._execute(command, **kwargs)
            status = result.get("status", "success")
        except Exception as e:
            log.error("agent_error", agent=self.name, error=str(e))
            result = {"status": "error", "error": str(e)}
            status = "error"

        # Audit
        duration = int((time.time() - start) * 1000)
        try:
            await self._audit.record(
                actor=self.name,
                action=command,
                command_id=command_id,
                result=status,
                duration_ms=duration,
            )
        except Exception:
            pass

        return result

    @abstractmethod
    async def _execute(self, command: str, **kwargs: Any) -> dict:
        """Override in subclasses with the actual action logic."""
        ...

    def _detect_intent(self, command: str) -> str:
        """Detect the intent of a command (basic keyword matching)."""
        cmd = command.lower()
        if any(w in cmd for w in ["check", "status", "health", "is"]):
            return "status_check"
        if any(w in cmd for w in ["restart", "fix", "repair"]):
            return "fix_action"
        if any(w in cmd for w in ["start", "launch", "run"]):
            return "start_action"
        if any(w in cmd for w in ["stop", "kill", "halt"]):
            return "stop_action"
        if any(w in cmd for w in ["list", "show", "get"]):
            return "query"
        if any(w in cmd for w in ["setup", "init", "configure"]):
            return "setup"
        return "unknown"

    def capabilities(self) -> dict[str, list[str]]:
        """Return the agent's capabilities."""
        return {
            "name": self.name,
            "description": self.description,
            "actions": [m for m in dir(self) if not m.startswith("_") and callable(getattr(self, m))],
        }
