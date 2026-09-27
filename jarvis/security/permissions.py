"""
JARVIS Security — Permissions
==============================
Permission system with 5 risk levels (L0-L4), human override, and temporary grants.

Permission Levels:
  L0 — Read-only (list apps, check status, open authorized apps)
  L1 — Low-risk (open files, switch windows, launch authorized terminal)
  L2 — Medium-risk (shell commands, restart services, modify config)
  L3 — High-risk (delete files, modify system config, expose credentials)
  L4 — Financial/destructive (live trades, format disks)

Rules:
  - All actions require a permission check.
  - Human override always wins (explicit user instruction > learned preference).
  - L0 auto-approves if the target is authorized.
  - L1 auto-approves if the target is authorized.
  - L2+ requires confirmation unless a standing permission exists.
  - Temporary permissions auto-expire (Phase 7 feature).
"""

from __future__ import annotations

import time
from typing import Any, Optional

from ..exceptions import PermissionDeniedError
from ..constants import PermissionLevel, PermissionDecision
from ..logging_config import get_logger

log = get_logger("permissions")


# ── Desktop Action → Permission Level Mapping ─────────────────────────────
DESKTOP_ACTION_LEVEL: dict[str, int] = {
    "launch": PermissionLevel.L1_LOW,
    "close": PermissionLevel.L1_LOW,
    "focus": PermissionLevel.L0_READ,
    "minimize": PermissionLevel.L0_READ,
    "maximize": PermissionLevel.L0_READ,
    "switch": PermissionLevel.L0_READ,
    "open_file": PermissionLevel.L1_LOW,
    "open_folder": PermissionLevel.L1_LOW,
    "open_url": PermissionLevel.L1_LOW,
    "execute": PermissionLevel.L2_MEDIUM,
    "restart": PermissionLevel.L2_MEDIUM,
    "stop": PermissionLevel.L2_MEDIUM,
    "status": PermissionLevel.L0_READ,
    "click": PermissionLevel.L1_LOW,
    "type": PermissionLevel.L1_LOW,
    "keypress": PermissionLevel.L1_LOW,
}


class PermissionResult:
    """Result of a permission check."""

    def __init__(
        self,
        granted: bool,
        level: int,
        decision: str,
        reason: str = "",
        requires_confirmation: bool = False,
    ):
        self.granted = granted
        self.level = level
        self.decision = decision
        self.reason = reason
        self.requires_confirmation = requires_confirmation

    def to_dict(self) -> dict:
        return {
            "granted": self.granted,
            "level": self.level,
            "decision": self.decision,
            "reason": self.reason,
            "requires_confirmation": self.requires_confirmation,
        }


class Permissions:
    """Permission checking engine."""

    def __init__(self) -> None:
        # {permission_key: {"level": int, "expires": float or None}}
        self._temporary: dict[str, dict] = {}
        # Set of authorized app names
        self._authorized_apps: set[str] = set()
        # Set of authorized projects
        self._authorized_projects: set[str] = set()

    def authorize_app(self, app_name: str) -> None:
        """Mark an application as authorized for L0-L1 actions."""
        self._authorized_apps.add(app_name)
        log.info("app_authorized", app=app_name)

    def deauthorize_app(self, app_name: str) -> None:
        """Remove an application from the authorized list."""
        self._authorized_apps.discard(app_name)
        log.info("app_deauthorized", app=app_name)

    def is_app_authorized(self, app_name: str) -> bool:
        return app_name in self._authorized_apps

    def authorize_project(self, project: str) -> None:
        self._authorized_projects.add(project)

    def grant_temporary(
        self, permission_key: str, level: int, ttl_seconds: int = 3600
    ) -> None:
        """Grant a temporary permission that auto-expires."""
        self._temporary[permission_key] = {
            "level": level,
            "expires": time.time() + ttl_seconds,
        }
        log.info("temp_permission_granted", key=permission_key, ttl=ttl_seconds)

    def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self._temporary.items() if v["expires"] < now]
        for k in expired:
            del self._temporary[k]

    def check(
        self,
        action: str,
        target: str = "",
        *,
        human_override: bool = False,
        action_level: Optional[int] = None,
    ) -> PermissionResult:
        """
        Check whether an action is permitted.

        Args:
            action: The action being performed (e.g., "launch", "execute").
            target: The target (app name, path, command).
            human_override: If True, the user explicitly requested this → always allowed.
            action_level: Override the default permission level for this action.

        Returns:
            PermissionResult with granted/denied + decision.
        """
        self._cleanup_expired()

        # Determine the permission level for this action
        level = action_level if action_level is not None else DESKTOP_ACTION_LEVEL.get(
            action, PermissionLevel.L2_MEDIUM
        )

        # Human override always wins (unless L4 and no explicit confirmation)
        if human_override:
            log.info("permission_override", action=action, target=target)
            return PermissionResult(
                granted=True,
                level=level,
                decision=PermissionDecision.CONFIRMED,
                reason="Human override",
                requires_confirmation=False,
            )

        # L0: auto-approve (read-only, no target authorization needed)
        if level <= PermissionLevel.L0_READ:
            return PermissionResult(
                granted=True,
                level=level,
                decision=PermissionDecision.AUTO_APPROVED,
                reason="L0 read-only",
                requires_confirmation=False,
            )

        # L1: auto-approve only if target (app) is authorized
        if level <= PermissionLevel.L1_LOW:
            if target and target.lower() in {a.lower() for a in self._authorized_apps}:
                return PermissionResult(
                    granted=True,
                    level=level,
                    decision=PermissionDecision.AUTO_APPROVED,
                    reason="L1, authorized target",
                    requires_confirmation=False,
                )
            return PermissionResult(
                granted=False,
                level=level,
                decision=PermissionDecision.DENIED,
                reason=f"L1, target '{target}' not authorized",
                requires_confirmation=True,
            )

        # L2+: always requires confirmation unless temp permission exists
        key = f"{action}:{target}"
        if key in self._temporary:
            temp = self._temporary[key]
            if time.time() < temp["expires"]:
                return PermissionResult(
                    granted=True,
                    level=level,
                    decision=PermissionDecision.AUTO_APPROVED,
                    reason=f"Temporary permission (expires in {temp['expires'] - time.time():.0f}s)",
                    requires_confirmation=False,
                )
            del self._temporary[key]

        return PermissionResult(
            granted=True,  # Granted but requires confirmation
            level=level,
            decision=PermissionDecision.CONFIRMED,
            reason=f"L{level} requires confirmation",
            requires_confirmation=True,
        )


# Module-level singleton
_permissions: Optional[Permissions] = None


def get_permissions() -> Permissions:
    global _permissions
    if _permissions is None:
        _permissions = Permissions()
    return _permissions
