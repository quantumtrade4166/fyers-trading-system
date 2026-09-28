"""
JARVIS Decisions
================
Decision memory — stores why decisions were made, alternatives considered, evidence, and context.

Supports:
  - "Why did we choose this?"
  - "Why did we abandon that?"
  - Historical reconstruction of decisions at a point in time
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from .database import Database, get_db
from ..exceptions import MemoryError
from ..logging_config import get_logger

log = get_logger("decisions")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_decision_id() -> str:
    return f"dec_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


class Decisions:
    """Decision CRUD operations."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db

    async def _get_db(self) -> Database:
        if self._db is not None:
            return self._db
        return await get_db()

    async def create(
        self,
        title: str,
        decision: str,
        reasoning: str = "",
        alternatives: str = "",
        evidence: str = "",
        context: str = "",
        project: Optional[str] = None,
        source_command_id: Optional[str] = None,
        decision_id: Optional[str] = None,
    ) -> dict:
        db = await self._get_db()
        did = decision_id or new_decision_id()
        now = utc_now()

        await db.execute(
            """
            INSERT INTO decisions
                (decision_id, title, decision, reasoning, alternatives,
                 evidence, context, project, source_command_id, made_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (did, title, decision, reasoning, alternatives, evidence, context,
             project, source_command_id, now),
        )
        log.info("decision_recorded", decision_id=did, title=title, project=project)
        return {
            "decision_id": did,
            "title": title,
            "decision": decision,
            "project": project,
            "made_at": now,
        }

    async def get(self, decision_id: str) -> Optional[dict]:
        db = await self._get_db()
        row = await db.fetch_one("SELECT * FROM decisions WHERE decision_id = ?", (decision_id,))
        return dict(row) if row else None

    async def get_for_project(self, project: str) -> list[dict]:
        db = await self._get_db()
        rows = await db.fetch_all(
            "SELECT * FROM decisions WHERE project = ? ORDER BY made_at DESC",
            (project,),
        )
        return [dict(r) for r in rows]

    async def search(self, query: str) -> list[dict]:
        """Full-text-like search across decision content."""
        db = await self._get_db()
        like = f"%{query}%"
        rows = await db.fetch_all(
            """SELECT * FROM decisions
               WHERE title LIKE ? OR decision LIKE ? OR reasoning LIKE ?
               ORDER BY made_at DESC""",
            (like, like, like),
        )
        return [dict(r) for r in rows]
