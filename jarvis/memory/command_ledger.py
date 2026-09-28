"""
JARVIS Command Ledger
=====================
Immutable command history. Every command the user gives JARVIS is recorded here.

Rules:
  - NEVER UPDATE. NEVER DELETE. Append-only.
  - The original command text is immutable once written.
  - Learned context/preferences are stored separately and may evolve.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..exceptions import ImmutableLedgerError
from ..logging_config import get_logger
from ..memory.database import Database, get_db

log = get_logger("command_ledger")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_command_id() -> str:
    return f"cmd_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


class CommandLedger:
    """Immutable command history."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db

    async def _get_db(self) -> Database:
        if self._db is not None:
            return self._db
        return await get_db()

    async def record(
        self,
        original_command: str,
        *,
        detected_intent: Optional[str] = None,
        target_project: Optional[str] = None,
        target_goal_id: Optional[str] = None,
        target_agent: Optional[str] = None,
        tools_used: Optional[str] = None,
        actions_taken: Optional[str] = None,
        result: Optional[str] = None,
        verification: Optional[str] = None,
        errors: Optional[str] = None,
        follow_up_state: Optional[str] = None,
        related_commands: Optional[str] = None,
        related_tasks: Optional[str] = None,
        related_projects: Optional[str] = None,
        command_id: Optional[str] = None,
        local_tz: str = "Australia/Sydney",
    ) -> dict:
        """
        Record a command in the immutable ledger.

        Returns the recorded command dict.
        """
        db = await self._get_db()
        cmd_id = command_id or new_command_id()
        now = utc_now()

        await db.execute(
            """
            INSERT INTO command_ledger
                (command_id, original_command, utc_timestamp, local_timestamp,
                 local_timezone, detected_intent, target_project, target_goal_id,
                 target_agent, tools_used, actions_taken, result, verification,
                 errors, follow_up_state, related_commands, related_tasks,
                 related_projects)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cmd_id,
                original_command,
                now,
                now,  # Phase 1: local = UTC. Fix when TZ is configurable.
                local_tz,
                detected_intent,
                target_project,
                target_goal_id,
                target_agent,
                tools_used,
                actions_taken,
                result,
                verification,
                errors,
                follow_up_state,
                related_commands,
                related_tasks,
                related_projects,
            ),
        )

        record = {
            "command_id": cmd_id,
            "original_command": original_command,
            "utc_timestamp": now,
            "detected_intent": detected_intent,
            "target_project": target_project,
            "result": result,
        }
        log.info("command_recorded", command_id=cmd_id, intent=detected_intent,
                 project=target_project)
        return record

    async def get(self, command_id: str) -> Optional[dict]:
        """Retrieve a single command by ID."""
        db = await self._get_db()
        return await db.fetch_one(
            "SELECT * FROM command_ledger WHERE command_id = ?",
            (command_id,),
        )

    async def get_all(
        self,
        *,
        project: Optional[str] = None,
        intent: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Query commands with optional filters."""
        db = await self._get_db()
        sql = "SELECT * FROM command_ledger WHERE 1=1"
        params: list = []

        if project is not None:
            sql += " AND target_project = ?"
            params.append(project)
        if intent is not None:
            sql += " AND detected_intent = ?"
            params.append(intent)

        sql += " ORDER BY utc_timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return await db.fetch_all(sql, tuple(params))

    async def count(self, *, project: Optional[str] = None) -> int:
        db = await self._get_db()
        sql = "SELECT COUNT(*) FROM command_ledger WHERE 1=1"
        params: list = []
        if project is not None:
            sql += " AND target_project = ?"
            params.append(project)
        result = await db.fetch_value(sql, tuple(params))
        return result or 0

    # ── NO update / NO delete — immutable by design ──────────────────────
    async def update(self, *args, **kwargs) -> None:
        raise ImmutableLedgerError("Command ledger is immutable", code="IMMUTABLE")

    async def delete(self, *args, **kwargs) -> None:
        raise ImmutableLedgerError("Command ledger is immutable", code="IMMUTABLE")


# Module-level singleton
_ledger: Optional[CommandLedger] = None


def get_ledger() -> CommandLedger:
    global _ledger
    if _ledger is None:
        _ledger = CommandLedger()
    return _ledger
