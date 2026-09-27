"""
JARVIS Tasks
============
Task system with dependencies, priorities, states, and project association.

Rules:
  - Tasks have states: pending, active, blocked, waiting, failed, completed, verified, overdue, stale.
  - Dependencies are tracked in task_dependencies (many-to-many).
  - Cycles in dependencies are detected on insertion.
  - All timestamps in UTC.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from .database import Database, get_db
from ..exceptions import MemoryError
from ..constants import TaskState
from ..logging_config import get_logger

log = get_logger("tasks")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_task_id() -> str:
    return f"task_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


class Tasks:
    """Task CRUD operations."""

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
        state: str = TaskState.PENDING,
        priority: int = 5,
        goal_id: Optional[str] = None,
        project: Optional[str] = None,
        deadline: Optional[str] = None,
        success_criteria: str = "",
        parent_task_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict:
        db = await self._get_db()
        tid = task_id or new_task_id()
        now = utc_now()

        await db.execute(
            """
            INSERT INTO tasks (task_id, title, description, state, priority,
                               goal_id, project, deadline, success_criteria,
                               parent_task_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tid, title, description, state, priority, goal_id, project,
             deadline, success_criteria, parent_task_id, now, now),
        )
        log.info("task_created", task_id=tid, state=state, project=project)
        return {
            "task_id": tid,
            "title": title,
            "state": state,
            "priority": priority,
            "goal_id": goal_id,
            "project": project,
            "created_at": now,
        }

    async def get(self, task_id: str) -> Optional[dict]:
        db = await self._get_db()
        row = await db.fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        return dict(row) if row else None

    async def get_all(
        self,
        *,
        state: Optional[str] = None,
        project: Optional[str] = None,
        goal_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        db = await self._get_db()
        sql = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if state is not None:
            sql += " AND state = ?"
            params.append(state)
        if project is not None:
            sql += " AND project = ?"
            params.append(project)
        if goal_id is not None:
            sql += " AND goal_id = ?"
            params.append(goal_id)
        sql += " ORDER BY priority ASC, created_at DESC LIMIT ?"
        params.append(limit)
        rows = await db.fetch_all(sql, tuple(params))
        return [dict(r) for r in rows]

    async def update(self, task_id: str, **fields) -> Optional[dict]:
        forbidden = {"task_id", "created_at"}
        for f in fields:
            if f in forbidden:
                raise MemoryError(f"Cannot update immutable field '{f}'", code="IMMUTABLE_FIELD")

        if fields:
            set_clauses = ", ".join(f"{k} = ?" for k in fields)
            params = list(fields.values()) + [utc_now(), task_id]
            db = await self._get_db()
            await db.execute(
                f"UPDATE tasks SET {set_clauses}, updated_at = ? WHERE task_id = ?",
                tuple(params),
            )
        return await self.get(task_id)

    async def set_state(self, task_id: str, state: str) -> Optional[dict]:
        if state == TaskState.COMPLETED:
            return await self.update(task_id, state=state, completed_at=utc_now())
        return await self.update(task_id, state=state)

    async def add_dependency(self, task_id: str, depends_on: str) -> bool:
        """
        Add a dependency: task_id depends on depends_on.
        Returns True if added, False if already exists, would create a cycle,
        or task depends on itself.
        """
        if task_id == depends_on:
            log.warning("self_dependency_prevented", task_id=task_id)
            return False

        db = await self._get_db()

        # Check for existing
        existing = await db.fetch_one(
            "SELECT 1 FROM task_dependencies WHERE task_id = ? AND depends_on = ?",
            (task_id, depends_on),
        )
        if existing:
            return False

        # Check for cycle: does depends_on transitively depend on task_id?
        if await self._would_cycle(db, task_id, depends_on):
            log.warning("dependency_cycle_prevented", task_id=task_id, depends_on=depends_on)
            return False

        await db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on) VALUES (?, ?)",
            (task_id, depends_on),
        )
        log.info("dependency_added", task_id=task_id, depends_on=depends_on)
        return True

    async def remove_dependency(self, task_id: str, depends_on: str) -> bool:
        db = await self._get_db()
        result = await db.execute(
            "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on = ?",
            (task_id, depends_on),
        )
        return result > 0

    async def get_dependencies(self, task_id: str) -> list[str]:
        db = await self._get_db()
        rows = await db.fetch_all(
            "SELECT depends_on FROM task_dependencies WHERE task_id = ?",
            (task_id,),
        )
        return [r["depends_on"] for r in rows]

    async def get_dependents(self, task_id: str) -> list[str]:
        db = await self._get_db()
        rows = await db.fetch_all(
            "SELECT task_id FROM task_dependencies WHERE depends_on = ?",
            (task_id,),
        )
        return [r["task_id"] for r in rows]

    async def _would_cycle(self, db: Database, task_id: str, depends_on: str) -> bool:
        """
        Check if adding task_id → depends_on would create a cycle.
        Uses DFS: if depends_on transitively depends on task_id, it's a cycle.
        """
        visited: set[str] = set()
        stack: list[str] = [depends_on]

        while stack:
            current = stack.pop()
            if current == task_id:
                return True
            if current in visited:
                continue
            visited.add(current)

            # Find what current depends on
            rows = await db.fetch_all(
                "SELECT depends_on FROM task_dependencies WHERE task_id = ?",
                (current,),
            )
            for r in rows:
                stack.append(r["depends_on"])

        return False


class TasksManager:
    """High-level tasks manager."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self.tasks = Tasks(db)
