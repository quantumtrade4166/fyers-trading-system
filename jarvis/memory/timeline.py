"""
JARVIS Timeline
===============
Unified chronological event stream across all sources.

Combines events from:
  - Commands (from command_ledger)
  - Goals (creation, completion, status changes)
  - Tasks (creation, completion, state changes)
  - Decisions
  - Agent actions
  - Desktop actions
  - External events (future: emails, calendar, trades)

Supports:
  - "What was I working on September 10?"
  - "What happened on this project today?"
  - Historical time machine (filter by timestamp range)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .database import Database, get_db
from ..exceptions import MemoryError
from ..logging_config import get_logger

log = get_logger("timeline")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_event_id() -> str:
    return f"evt_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


class Timeline:
    """Timeline event management."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db

    async def _get_db(self) -> Database:
        if self._db is not None:
            return self._db
        return await get_db()

    async def add(
        self,
        event_type: str,
        title: str,
        *,
        summary: str = "",
        details: str = "",
        source: Optional[str] = None,
        project: Optional[str] = None,
        command_id: Optional[str] = None,
        goal_id: Optional[str] = None,
        task_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> dict:
        """Add an event to the timeline."""
        db = await self._get_db()
        eid = event_id or new_event_id()
        ts = timestamp or utc_now()

        await db.execute(
            """
            INSERT INTO timeline
                (event_id, timestamp, event_type, source, project,
                 title, summary, details, command_id, goal_id, task_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (eid, ts, event_type, source, project, title, summary,
             details, command_id, goal_id, task_id),
        )
        return {
            "event_id": eid,
            "timestamp": ts,
            "event_type": event_type,
            "title": title,
            "project": project,
        }

    async def get(
        self,
        *,
        project: Optional[str] = None,
        event_type: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Query timeline events."""
        db = await self._get_db()
        sql = "SELECT * FROM timeline WHERE 1=1"
        params: list = []

        if project is not None:
            sql += " AND project = ?"
            params.append(project)
        if event_type is not None:
            sql += " AND event_type = ?"
            params.append(event_type)
        if start is not None:
            sql += " AND timestamp >= ?"
            params.append(start)
        if end is not None:
            sql += " AND timestamp <= ?"
            params.append(end)

        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = await db.fetch_all(sql, tuple(params))
        return [dict(r) for r in rows]

    async def count(self, *, project: Optional[str] = None) -> int:
        db = await self._get_db()
        sql = "SELECT COUNT(*) FROM timeline WHERE 1=1"
        params: list = []
        if project is not None:
            sql += " AND project = ?"
            params.append(project)
        result = await db.fetch_value(sql, tuple(params))
        return result or 0
