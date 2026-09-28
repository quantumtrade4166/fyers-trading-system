"""
JARVIS Goals
============
Goal system with milestones, deadlines, success criteria, and progress tracking.

Rules:
  - Goals have status (planning, active, at_risk, blocked, completed, cancelled).
  - Milestones belong to a goal.
  - Goals can be superseded by later goals (not deleted).
  - All timestamps in UTC.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .database import Database, get_db
from ..exceptions import MemoryError
from ..constants import GoalStatus
from ..logging_config import get_logger

log = get_logger("goals")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_goal_id() -> str:
    return f"goal_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


def new_milestone_id() -> str:
    return f"ms_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


class Goals:
    """Goal CRUD operations."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db

    async def _get_db(self) -> Database:
        if self._db is not None:
            return self._db
        return await get_db()

    async def create(
        self,
        title: str,
        description: str = "",
        priority: int = 5,
        deadline: Optional[str] = None,
        rationale: str = "",
        success_criteria: str = "",
        project: Optional[str] = None,
        goal_id: Optional[str] = None,
    ) -> dict:
        db = await self._get_db()
        gid = goal_id or new_goal_id()
        now = utc_now()

        await db.execute(
            """
            INSERT INTO goals (goal_id, title, description, priority, deadline,
                               rationale, success_criteria, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (gid, title, description, priority, deadline, rationale, success_criteria, now, now),
        )

        record = {
            "goal_id": gid,
            "title": title,
            "description": description,
            "status": GoalStatus.PLANNING,
            "priority": priority,
            "deadline": deadline,
            "rationale": rationale,
            "success_criteria": success_criteria,
            "progress_pct": 0,
            "created_at": now,
        }
        log.info("goal_created", goal_id=gid, title=title, project=project)
        return record

    async def get(self, goal_id: str) -> Optional[dict]:
        db = await self._get_db()
        row = await db.fetch_one("SELECT * FROM goals WHERE goal_id = ?", (goal_id,))
        return dict(row) if row else None

    async def get_all(self, *, status: Optional[str] = None, project: Optional[str] = None) -> list[dict]:
        db = await self._get_db()
        sql = "SELECT * FROM goals WHERE 1=1"
        params: list = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if project is not None:
            sql += " AND project = ?"
            params.append(project)
        sql += " ORDER BY priority ASC, created_at DESC"
        rows = await db.fetch_all(sql, tuple(params))
        return [dict(r) for r in rows]

    async def update(self, goal_id: str, **fields) -> Optional[dict]:
        """Update mutable fields on a goal. Never update immutable fields."""
        if not fields:
            return await self.get(goal_id)

        # Never allow updating goal_id, created_at
        forbidden = {"goal_id", "created_at"}
        for f in fields:
            if f in forbidden:
                raise MemoryError(f"Cannot update immutable field '{f}'", code="IMMUTABLE_FIELD")

        set_clauses = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [utc_now(), goal_id]

        db = await self._get_db()
        await db.execute(
            f"UPDATE goals SET {set_clauses}, updated_at = ? WHERE goal_id = ?",
            tuple(params),
        )
        return await self.get(goal_id)

    async def complete(self, goal_id: str) -> Optional[dict]:
        now = utc_now()
        return await self.update(goal_id, status=GoalStatus.COMPLETED, completed_at=now, progress_pct=100)

    async def cancel(self, goal_id: str, reason: str = "") -> Optional[dict]:
        return await self.update(goal_id, status=GoalStatus.CANCELLED)

    async def set_progress(self, goal_id: str, pct: int) -> Optional[dict]:
        if pct < 0 or pct > 100:
            raise MemoryError(f"Progress must be 0-100, got {pct}", code="BAD_PROGRESS")
        return await self.update(goal_id, progress_pct=pct)


class Milestones:
    """Milestone CRUD operations."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db

    async def _get_db(self) -> Database:
        if self._db is not None:
            return self._db
        return await get_db()

    async def create(
        self,
        goal_id: str,
        title: str,
        description: str = "",
        due_date: Optional[str] = None,
        ms_id: Optional[str] = None,
    ) -> dict:
        db = await self._get_db()
        mid = ms_id or new_milestone_id()
        now = utc_now()

        await db.execute(
            """
            INSERT INTO milestones (milestone_id, goal_id, title, description, due_date, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (mid, goal_id, title, description, due_date),
        )
        return {"milestone_id": mid, "goal_id": goal_id, "title": title, "status": "pending"}

    async def get_for_goal(self, goal_id: str) -> list[dict]:
        db = await self._get_db()
        rows = await db.fetch_all(
            "SELECT * FROM milestones WHERE goal_id = ? ORDER BY created_at",
            (goal_id,),
        )
        return [dict(r) for r in rows]

    async def complete(self, ms_id: str) -> Optional[dict]:
        now = utc_now()
        db = await self._get_db()
        await db.execute(
            "UPDATE milestones SET status = 'completed', completed_at = ? WHERE milestone_id = ?",
            (now, ms_id),
        )
        row = await db.fetch_one("SELECT * FROM milestones WHERE milestone_id = ?", (ms_id,))
        return dict(row) if row else None


class GoalsManager:
    """High-level goals manager with milestones support."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self.goals = Goals(db)
        self.milestones = Milestones(db)
