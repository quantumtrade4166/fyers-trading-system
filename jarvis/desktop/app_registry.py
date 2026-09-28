"""
JARVIS Desktop — Application Registry
=======================================
Persistent registry of applications JARVIS can control.

Each application has:
  - Name and aliases (e.g., "Claude Code", "claude", "cc")
  - Executable path (auto-discovered if not provided)
  - Allowed actions
  - Permission level
  - Whether GUI/CLI/API automation is available
  - Status (running/not_running)

Default seed includes common Windows apps used by the user.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..exceptions import AppNotFoundError
from ..constants import AppStatus, HealthStatus, DesktopAction, PermissionLevel
from ..memory.database import Database, get_db
from ..logging_config import get_logger

log = get_logger("app_registry")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AppRegistry:
    """Application registry with CRUD and discovery."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db

    async def _get_db(self) -> Database:
        if self._db is not None:
            return self._db
        return await get_db()

    async def register(
        self,
        name: str,
        *,
        aliases: list[str] | str = "",
        exe_path: str = "",
        app_id: str = "",
        os_name: str = "windows",
        install_location: str = "",
        associated_projects: list[str] | str = "",
        workflows: list[str] | str = "",
        allowed_actions: list[str] | None = None,
        permission_level: int = PermissionLevel.L1_LOW,
        gui_automation: bool = False,
        cli_automation: bool = False,
        api_available: bool = False,
        version: str = "",
        launch_args: str = "",
        verify_process: str = "",
    ) -> dict:
        """Register an application."""
        db = await self._get_db()
        now = utc_now()

        # Serialize JSON fields
        if isinstance(aliases, list):
            aliases = json.dumps(aliases)
        if isinstance(associated_projects, list):
            associated_projects = json.dumps(associated_projects)
        if isinstance(workflows, list):
            workflows = json.dumps(workflows)
        if allowed_actions is None:
            allowed_actions = [DesktopAction.LAUNCH, DesktopAction.FOCUS, DesktopAction.STATUS]
        if isinstance(allowed_actions, list):
            allowed_actions = json.dumps(allowed_actions)

        await db.execute(
            """
            INSERT INTO app_registry
                (name, aliases, exe_path, app_id, os, install_location,
                 associated_projects, workflows, allowed_actions, permission_level,
                 gui_automation, cli_automation, api_available,
                 last_known_status, health_status, version, launch_args,
                 verify_process, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name, aliases, exe_path, app_id, os_name, install_location,
                associated_projects, workflows, allowed_actions, permission_level,
                int(gui_automation), int(cli_automation), int(api_available),
                AppStatus.UNKNOWN, HealthStatus.UNKNOWN,
                version, launch_args, verify_process, now, now,
            ),
        )
        log.info("app_registered", name=name, os=os_name)
        return {"name": name, "exe_path": exe_path, "permission_level": permission_level}

    async def get(self, name: str, os_name: str = "windows") -> Optional[dict]:
        """Get an app by name (exact match)."""
        db = await self._get_db()
        row = await db.fetch_one(
            "SELECT * FROM app_registry WHERE name = ? AND os = ?",
            (name, os_name),
        )
        if row:
            return dict(row)
        # Try alias
        alias_match = await db.fetch_all(
            "SELECT * FROM app_registry WHERE os = ? AND aliases LIKE ?",
            (os_name, f"%{name}%"),
        )
        if alias_match:
            return dict(alias_match[0])
        return None

    async def resolve(self, query: str, os_name: str = "windows") -> list[dict]:
        """Resolve a query (name or alias) to all matching apps."""
        db = await self._get_db()
        like = f"%{query}%"
        rows = await db.fetch_all(
            "SELECT * FROM app_registry WHERE os = ? AND (name LIKE ? OR aliases LIKE ?)",
            (os_name, query, like),
        )
        return [dict(r) for r in rows]

    async def list_all(self, os_name: str = "windows") -> list[dict]:
        """List all registered apps."""
        db = await self._get_db()
        rows = await db.fetch_all(
            "SELECT * FROM app_registry WHERE os = ? ORDER BY name",
            (os_name,),
        )
        return [dict(r) for r in rows]

    async def update_status(
        self, name: str, status: str, os_name: str = "windows"
    ) -> bool:
        """Update the last known status of an app."""
        db = await self._get_db()
        now = utc_now()
        result = await db.execute(
            "UPDATE app_registry SET last_known_status = ?, updated_at = ? "
            "WHERE name = ? AND os = ?",
            (status, now, name, os_name),
        )
        return result > 0

    async def update_health(
        self, name: str, health: str, os_name: str = "windows"
    ) -> bool:
        db = await self._get_db()
        now = utc_now()
        result = await db.execute(
            "UPDATE app_registry SET health_status = ?, updated_at = ? "
            "WHERE name = ? AND os = ?",
            (health, now, name, os_name),
        )
        return result > 0


# ── Default Seed Apps ─────────────────────────────────────────────────────
# Common applications used by the user. Path discovery happens at runtime.
DEFAULT_APPS = [
    {
        "name": "Claude Code",
        "aliases": ["claude", "cc", "claude-code"],
        "exe_path": "claude",  # PATH-based; resolved at launch
        "permission_level": PermissionLevel.L1_LOW,
        "allowed_actions": ["launch", "focus", "close", "status", "switch"],
        "cli_automation": True,
        "associated_projects": ["fyers_data_pipeline", "jarvis"],
    },
    {
        "name": "VS Code",
        "aliases": ["vscode", "code", "vs"],
        "exe_path": "code",
        "permission_level": PermissionLevel.L1_LOW,
        "allowed_actions": ["launch", "focus", "close", "status", "switch"],
        "cli_automation": True,
        "associated_projects": ["fyers_data_pipeline", "jarvis"],
    },
    {
        "name": "Obsidian",
        "aliases": ["obsidian"],
        "exe_path": "obsidian",
        "permission_level": PermissionLevel.L1_LOW,
        "allowed_actions": ["launch", "focus", "close", "status", "switch"],
        "associated_projects": ["trading_brain"],
    },
    {
        "name": "Windows Terminal",
        "aliases": ["terminal", "wt", "cmd"],
        "exe_path": "wt.exe",
        "permission_level": PermissionLevel.L1_LOW,
        "allowed_actions": ["launch", "focus", "close", "status", "switch"],
        "cli_automation": True,
    },
    {
        "name": "PowerShell",
        "aliases": ["powershell", "ps"],
        "exe_path": "powershell.exe",
        "permission_level": PermissionLevel.L1_LOW,
        "allowed_actions": ["launch", "focus", "close", "status", "switch", "execute"],
        "cli_automation": True,
    },
    {
        "name": "Chrome",
        "aliases": ["chrome", "browser", "google-chrome"],
        "exe_path": "chrome.exe",
        "permission_level": PermissionLevel.L1_LOW,
        "allowed_actions": ["launch", "focus", "close", "status", "switch", "open_url"],
        "cli_automation": True,
    },
    {
        "name": "File Explorer",
        "aliases": ["explorer", "files"],
        "exe_path": "explorer.exe",
        "permission_level": PermissionLevel.L1_LOW,
        "allowed_actions": ["launch", "focus", "close", "status", "open_folder"],
        "cli_automation": True,
    },
    {
        "name": "Git",
        "aliases": ["git"],
        "exe_path": "git",
        "permission_level": PermissionLevel.L0_READ,
        "allowed_actions": ["status"],
        "cli_automation": True,
    },
    {
        "name": "Python",
        "aliases": ["python"],
        "exe_path": "python",
        "permission_level": PermissionLevel.L0_READ,
        "allowed_actions": ["status"],
        "cli_automation": True,
    },
    {
        "name": "JARVIS",
        "aliases": ["jarvis"],
        "exe_path": "jarvis",
        "permission_level": PermissionLevel.L0_READ,
        "allowed_actions": ["status"],
        "cli_automation": True,
    },
    {
        "name": "Task Scheduler",
        "aliases": ["taskschd", "schtasks"],
        "exe_path": "schtasks.exe",
        "permission_level": PermissionLevel.L0_READ,
        "allowed_actions": ["status"],
        "cli_automation": True,
    },
]


async def seed_default_apps(registry: Optional[AppRegistry] = None) -> int:
    """Seed the registry with default applications. Idempotent."""
    reg = registry or AppRegistry()
    seeded = 0
    for app in DEFAULT_APPS:
        existing = await reg.get(app["name"])
        if existing:
            continue
        try:
            await reg.register(**app)
            seeded += 1
        except Exception as e:
            log.warning("seed_app_failed", name=app["name"], error=str(e))
    log.info("apps_seeded", count=seeded, total=len(DEFAULT_APPS))
    return seeded
