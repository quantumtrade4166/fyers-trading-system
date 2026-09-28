"""
JARVIS Security — Kill Switch
==============================
Emergency stop mechanism. When activated, blocks all non-read operations.

Trigger:
  - File-based: creating jarvis/data/kill_switch.flag
  - API: POST /api/kill-switch (Phase 2+)
  - Voice: "JARVIS, stop everything." (Phase 10+)

When active:
  - All L1-L4 actions are blocked.
  - Agents are halted where technically possible.
  - Desktop control is suspended.
  - A human override can deactivate the kill switch.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from ..exceptions import KillSwitchActiveError
from ..constants import KILL_SWITCH_FILE
from ..logging_config import get_logger

log = get_logger("kill_switch")


class KillSwitch:
    """Emergency stop controller."""

    def __init__(self, flag_path: str = KILL_SWITCH_FILE) -> None:
        self.flag_path = Path(flag_path)
        self._active = False

    @property
    def is_active(self) -> bool:
        """Check if kill switch is currently active."""
        if self._active:
            return True
        # Also check the flag file (survives restarts)
        return self.flag_path.exists()

    def activate(self) -> None:
        """Activate the kill switch."""
        self._active = True
        self.flag_path.parent.mkdir(parents=True, exist_ok=True)
        self.flag_path.write_text(f"KILL_SWITCH activated at {self._now_iso()}\n")
        log.critical("KILL_SWITCH_ACTIVATED", path=str(self.flag_path))

    def deactivate(self) -> None:
        """Deactivate the kill switch (human override)."""
        self._active = False
        if self.flag_path.exists():
            self.flag_path.unlink()
        log.warning("KILL_SWITCH_DEACTIVATED")

    def check(self, action: str) -> None:
        """
        Check if the action is allowed. Raises if kill switch is active.
        L0 (read-only) actions are always allowed.
        """
        if self._active or self.flag_path.exists():
            # L0 is always allowed even when killed
            log.warning("kill_switch_blocked", action=action)
            raise KillSwitchActiveError(
                f"Kill switch is active. Action '{action}' is blocked. "
                "Say 'JARVIS, resume' or remove the kill switch flag.",
                code="KILL_SWITCH_ACTIVE",
            )

    def _now_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


# Module-level singleton
_kill_switch: Optional[KillSwitch] = None


def get_kill_switch() -> KillSwitch:
    global _kill_switch
    if _kill_switch is None:
        _kill_switch = KillSwitch()
    return _kill_switch
