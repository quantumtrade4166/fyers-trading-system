"""
JARVIS Desktop — Project to Application Mapping
=================================================
Maps projects to the applications that work with them.

Examples:
  fyers_data_pipeline → VS Code, Claude Code, Terminal, Git
  jarvis → VS Code, Claude Code, Terminal
  trading_brain → Obsidian, VS Code

This allows natural commands like:
  "Open the trading project" → JARVIS knows to open Claude Code + VS Code + Terminal in fyers_data_pipeline/
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..memory.database import Database, get_db
from ..logging_config import get_logger

log = get_logger("project_map")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Default Project Mappings ──────────────────────────────────────────────
DEFAULT_PROJECTS = [
    {
        "name": "fyers_data_pipeline",
        "aliases": ["trading", "fyers", "fyers-data-pipeline"],
        "repository": "https://github.com/quantumtrade4166/fyers-trading-system",
        "working_dir": "G:/fyers_data_pipeline",
        "default_branch": "main",
        "obsidian_folder": "Trading System",
        "documentation": "https://github.com/quantumtrade4166/fyers-trading-system",
        "health": "healthy",
    },
    {
        "name": "jarvis",
        "aliases": ["jarvis"],
        "repository": "https://github.com/quantumtrade4166/fyers-trading-system",
        "working_dir": "G:/fyers_data_pipeline/jarvis",
        "default_branch": "main",
        "obsidian_folder": "JARVIS",
        "documentation": "G:/fyers_data_pipeline/jarvis/docs/ARCHITECTURE.md",
        "health": "healthy",
    },
    {
        "name": "trading_brain",
        "aliases": ["vault", "obsidian", "trading-brain"],
        "repository": "",
        "working_dir": "G:/Trading Brain",
        "default_branch": "",
        "obsidian_folder": "Trading Brain",
        "documentation": "G:/Trading Brain",
        "health": "healthy",
    },
]


DEFAULT_PROJECT_MAP = [
    {
        "project": "fyers_data_pipeline",
        "app_name": "Claude Code",
        "role": "primary",
        "auto_open": True,
    },
    {
        "project": "fyers_data_pipeline",
        "app_name": "VS Code",
        "role": "primary",
        "auto_open": True,
    },
    {
        "project": "fyers_data_pipeline",
        "app_name": "Windows Terminal",
        "role": "primary",
        "auto_open": False,
    },
    {
        "project": "fyers_data_pipeline",
        "app_name": "Git",
        "role": "tool",
        "auto_open": False,
    },
    {
        "project": "jarvis",
        "app_name": "Claude Code",
        "role": "primary",
        "auto_open": True,
    },
    {
        "project": "jarvis",
        "app_name": "VS Code",
        "role": "primary",
        "auto_open": True,
    },
    {
        "project": "jarvis",
        "app_name": "Windows Terminal",
        "role": "primary",
        "auto_open": False,
    },
    {
        "project": "trading_brain",
        "app_name": "Obsidian",
        "role": "primary",
        "auto_open": True,
    },
]


class Projects:
    """Project registry."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db

    async def _get_db(self) -> Database:
        if self._db is not None:
            return self._db
        return await get_db()

    async def create(
        self,
        name: str,
        aliases: list[str] | str = "",
        repository: str = "",
        working_dir: str = "",
        default_branch: str = "main",
        obsidian_folder: str = "",
        documentation: str = "",
        health: str = "healthy",
    ) -> dict:
        db = await self._get_db()
        now = utc_now()
        import json as _json
        if isinstance(aliases, list):
            aliases = _json.dumps(aliases)

        pid = f"proj_{name}"
        await db.execute(
            """INSERT INTO projects
               (project_id, name, aliases, repository, working_dir, default_branch,
                obsidian_folder, documentation, health, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pid, name, aliases, repository, working_dir, default_branch,
             obsidian_folder, documentation, health, now, now),
        )
        log.info("project_created", name=name, working_dir=working_dir)
        return {"project_id": pid, "name": name, "working_dir": working_dir}

    async def get(self, name_or_id: str) -> Optional[dict]:
        db = await self._get_db()
        # Try exact match
        row = await db.fetch_one(
            "SELECT * FROM projects WHERE name = ? OR project_id = ?",
            (name_or_id, name_or_id),
        )
        if row:
            return dict(row)
        # Try alias
        rows = await db.fetch_all(
            "SELECT * FROM projects WHERE aliases LIKE ?",
            (f"%{name_or_id}%",),
        )
        if rows:
            return dict(rows[0])
        return None

    async def list_all(self) -> list[dict]:
        db = await self._get_db()
        rows = await db.fetch_all("SELECT * FROM projects ORDER BY name")
        return [dict(r) for r in rows]


class ProjectMap:
    """Project-to-application mapping."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db

    async def _get_db(self) -> Database:
        if self._db is not None:
            return self._db
        return await get_db()

    async def add(
        self,
        project_id: str,
        app_name: str,
        os_name: str = "windows",
        role: str = "",
        workspace_arg: str = "",
        auto_open: bool = False,
        notes: str = "",
    ) -> dict:
        db = await self._get_db()
        now = utc_now()
        await db.execute(
            """INSERT OR REPLACE INTO project_map
               (project_id, app_name, os, role, workspace_arg, auto_open, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, app_name, os_name, role, workspace_arg, int(auto_open), notes, now),
        )
        return {"project_id": project_id, "app_name": app_name, "role": role}

    async def apps_for_project(self, project_id: str) -> list[dict]:
        db = await self._get_db()
        rows = await db.fetch_all(
            "SELECT * FROM project_map WHERE project_id = ? ORDER BY auto_open DESC, role",
            (project_id,),
        )
        return [dict(r) for r in rows]


async def seed_default_projects() -> int:
    """Seed default projects and their app mappings. Idempotent."""
    projects = Projects()
    project_map = ProjectMap()
    seeded = 0

    for p in DEFAULT_PROJECTS:
        existing = await projects.get(p["name"])
        if not existing:
            await projects.create(**p)
            seeded += 1

    for m in DEFAULT_PROJECT_MAP:
        proj = await projects.get(m["project"])
        if proj:
            existing_maps = await project_map.apps_for_project(proj["project_id"])
            if not any(em["app_name"] == m["app_name"] for em in existing_maps):
                await project_map.add(
                    project_id=proj["project_id"],
                    app_name=m["app_name"],
                    role=m["role"],
                    auto_open=m["auto_open"],
                )
    log.info("projects_seeded", count=seeded)
    return seeded
