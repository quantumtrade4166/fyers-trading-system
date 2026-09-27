"""
JARVIS Database Layer
=====================
SQLite database initialization, connection management, and schema.

Rules:
  - WAL mode for concurrent reads with a single writer.
  - One writer at a time (acceptable for Phase 1).
  - Parameterized queries only. No string interpolation of values.
  - Immutable tables (command_ledger, audit_trail, desktop_actions)
    have NO update or delete code paths anywhere in jarvis/.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from ..config import get_config
from ..exceptions import DatabaseError, SchemaError
from ..logging_config import get_logger

log = get_logger("database")

# ── Schema Version ────────────────────────────────────────────────────────
SCHEMA_VERSION = 1


# ── Schema Definitions ────────────────────────────────────────────────────
SCHEMA: list[str] = [
    # ── Immutable command history (Req 9) ─────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS command_ledger (
        command_id      TEXT PRIMARY KEY,
        original_command TEXT NOT NULL,
        utc_timestamp   TEXT NOT NULL,
        local_timestamp TEXT,
        local_timezone  TEXT,
        detected_intent TEXT,
        target_project  TEXT,
        target_goal_id  TEXT,
        target_agent    TEXT,
        tools_used      TEXT,
        actions_taken   TEXT,
        result          TEXT,
        verification    TEXT,
        errors          TEXT,
        follow_up_state TEXT,
        related_commands TEXT,
        related_tasks   TEXT,
        related_projects TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,

    # ── Append-only audit trail (Req 41) ─────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS audit_trail (
        seq           INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id      TEXT NOT NULL UNIQUE,
        prev_hash     TEXT,
        entry_hash    TEXT NOT NULL,
        command_id    TEXT,
        timestamp     TEXT NOT NULL,
        actor         TEXT NOT NULL,
        action        TEXT NOT NULL,
        permission_level INTEGER,
        permission_decision TEXT,
        confirmation_status TEXT DEFAULT 'not_required',
        target        TEXT,
        details       TEXT,
        result        TEXT,
        verification  TEXT,
        error_info    TEXT,
        stdout        TEXT,
        stderr        TEXT,
        duration_ms   INTEGER,
        follow_up_action TEXT
    )
    """,

    # ── Goals (Req 17) ───────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS goals (
        goal_id        TEXT PRIMARY KEY,
        title          TEXT NOT NULL,
        description    TEXT,
        status         TEXT NOT NULL DEFAULT 'planning',
        priority       INTEGER DEFAULT 5,
        rationale      TEXT,
        deadline       TEXT,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
        completed_at   TEXT,
        success_criteria TEXT,
        progress_pct   INTEGER DEFAULT 0,
        risks          TEXT,
        superseded_by  TEXT
    )
    """,

    # ── Milestones (Req 99) ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS milestones (
        milestone_id   TEXT PRIMARY KEY,
        goal_id        TEXT NOT NULL,
        title          TEXT NOT NULL,
        description    TEXT,
        due_date       TEXT,
        completed_at   TEXT,
        status         TEXT NOT NULL DEFAULT 'pending',
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
    )
    """,

    # ── Tasks (Req 18) ───────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id        TEXT PRIMARY KEY,
        title          TEXT NOT NULL,
        description    TEXT,
        state          TEXT NOT NULL DEFAULT 'pending',
        priority       INTEGER DEFAULT 5,
        goal_id        TEXT,
        project        TEXT,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
        completed_at   TEXT,
        deadline       TEXT,
        success_criteria TEXT,
        blocker        TEXT,
        waiting_for    TEXT,
        parent_task_id TEXT,
        FOREIGN KEY (goal_id) REFERENCES goals(goal_id),
        FOREIGN KEY (parent_task_id) REFERENCES tasks(task_id)
    )
    """,

    # ── Task dependencies (Req 18) ───────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS task_dependencies (
        task_id        TEXT NOT NULL,
        depends_on     TEXT NOT NULL,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (task_id, depends_on),
        FOREIGN KEY (task_id) REFERENCES tasks(task_id),
        FOREIGN KEY (depends_on) REFERENCES tasks(task_id)
    )
    """,

    # ── Decisions (Req 50) ───────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS decisions (
        decision_id    TEXT PRIMARY KEY,
        title          TEXT NOT NULL,
        decision       TEXT NOT NULL,
        reasoning      TEXT,
        alternatives   TEXT,
        evidence       TEXT,
        context        TEXT,
        project        TEXT,
        made_at        TEXT NOT NULL DEFAULT (datetime('now')),
        source_command_id TEXT,
        superseded_by  TEXT,
        FOREIGN KEY (source_command_id) REFERENCES command_ledger(command_id)
    )
    """,

    # ── Unified timeline (Req 48) ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS timeline (
        event_id       TEXT PRIMARY KEY,
        timestamp      TEXT NOT NULL,
        event_type     TEXT NOT NULL,
        source         TEXT,
        project        TEXT,
        title          TEXT NOT NULL,
        summary        TEXT,
        details        TEXT,
        command_id     TEXT,
        goal_id        TEXT,
        task_id        TEXT,
        created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,

    # ── Projects (Req 13) ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS projects (
        project_id     TEXT PRIMARY KEY,
        name           TEXT NOT NULL UNIQUE,
        aliases        TEXT,
        repository     TEXT,
        working_dir    TEXT,
        git_remote     TEXT,
        default_branch TEXT,
        obsidian_folder TEXT,
        documentation  TEXT,
        health         TEXT NOT NULL DEFAULT 'healthy',
        last_activity  TEXT,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,

    # ── Application Registry (DC-12) ─────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS app_registry (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        name              TEXT NOT NULL,
        aliases           TEXT,
        exe_path          TEXT,
        app_id            TEXT,
        os                TEXT NOT NULL DEFAULT 'windows',
        install_location  TEXT,
        associated_projects TEXT,
        workflows         TEXT,
        allowed_actions   TEXT,
        permission_level  INTEGER NOT NULL DEFAULT 0,
        gui_automation    INTEGER NOT NULL DEFAULT 0,
        cli_automation    INTEGER NOT NULL DEFAULT 0,
        api_available     INTEGER NOT NULL DEFAULT 0,
        last_known_status TEXT DEFAULT 'unknown',
        health_status     TEXT DEFAULT 'unknown',
        version           TEXT,
        launch_args       TEXT,
        verify_process    TEXT,
        created_at        TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(name, os)
    )
    """,

    # ── Project-to-Application Mapping (DC-13) ───────────────────────────
    """
    CREATE TABLE IF NOT EXISTS project_map (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id   TEXT NOT NULL,
        app_name     TEXT NOT NULL,
        os           TEXT NOT NULL DEFAULT 'windows',
        role         TEXT,
        workspace_arg TEXT,
        auto_open    INTEGER NOT NULL DEFAULT 0,
        notes        TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(project_id, app_name, os),
        FOREIGN KEY (project_id) REFERENCES projects(project_id)
    )
    """,

    # ── Preferences (Req 68) ─────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS preferences (
        pref_id      TEXT PRIMARY KEY,
        category     TEXT NOT NULL,
        key          TEXT NOT NULL,
        value        TEXT NOT NULL,
        source       TEXT NOT NULL,
        confidence   TEXT DEFAULT 'explicit',
        active       INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(category, key)
    )
    """,

    # ── Commitments (Req 21) ─────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS commitments (
        commitment_id TEXT PRIMARY KEY,
        description   TEXT NOT NULL,
        promised_to   TEXT,
        due_date      TEXT,
        project       TEXT,
        status        TEXT NOT NULL DEFAULT 'open',
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        fulfilled_at  TEXT,
        source_command_id TEXT,
        FOREIGN KEY (source_command_id) REFERENCES command_ledger(command_id)
    )
    """,
]

INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_cmd_ts ON command_ledger(utc_timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_cmd_project ON command_ledger(target_project)",
    "CREATE INDEX IF NOT EXISTS idx_audit_cmd ON audit_trail(command_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_trail(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_goal_status ON goals(status)",
    "CREATE INDEX IF NOT EXISTS idx_task_state ON tasks(state)",
    "CREATE INDEX IF NOT EXISTS idx_task_goal ON tasks(goal_id)",
    "CREATE INDEX IF NOT EXISTS idx_timeline_ts ON timeline(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_timeline_type ON timeline(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_decision_project ON decisions(project)",
    "CREATE INDEX IF NOT EXISTS idx_app_name ON app_registry(name)",
    "CREATE INDEX IF NOT EXISTS idx_map_project ON project_map(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_pref_key ON preferences(category, key)",
    "CREATE INDEX IF NOT EXISTS idx_commit_due ON commitments(due_date)",
]


# ── Schema Metadata Table ─────────────────────────────────────────────────
_SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_info (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""


# ── Database Class ────────────────────────────────────────────────────────
class Database:
    """
    JARVIS SQLite database manager.

    Usage (async):
        db = Database()
        await db.initialize()
        rows = await db.fetch_all("SELECT * FROM goals", ())
        await db.close()
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or get_config().db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Open the connection, enable WAL, create schema. Idempotent."""
        if self._initialized and self._conn is not None:
            return

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        # Enforce foreign keys
        await self._conn.execute("PRAGMA foreign_keys = ON")
        # WAL mode for concurrent reads
        if get_config().db_wal_mode:
            await self._conn.execute("PRAGMA journal_mode = WAL")
        # Busy timeout
        await self._conn.execute(f"PRAGMA busy_timeout = {get_config().db_busy_timeout_ms}")

        await self._create_schema()
        await self._conn.commit()
        self._initialized = True
        log.info("database_initialized", path=self.db_path, wal=get_config().db_wal_mode,
                 version=SCHEMA_VERSION)

    async def _create_schema(self) -> None:
        """Create all tables and indexes if they don't exist."""
        if self._conn is None:
            raise DatabaseError("Connection not open", code="NO_CONNECTION")

        try:
            await self._conn.execute(_SCHEMA_VERSION_TABLE)
            for statement in SCHEMA:
                await self._conn.execute(statement)
            for statement in INDEXES:
                await self._conn.execute(statement)

            # Record schema version
            await self._conn.execute(
                "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
                ("version", str(SCHEMA_VERSION)),
            )
        except sqlite3.Error as e:
            raise SchemaError(f"Schema creation failed: {e}", code="SCHEMA_FAILED") from e

    # ── Query Helpers ─────────────────────────────────────────────────────
    async def execute(self, sql: str, params: tuple = ()) -> int:
        """Execute INSERT/UPDATE/DELETE. Returns rowcount."""
        if self._conn is None:
            raise DatabaseError("Connection not open", code="NO_CONNECTION")
        async with self._lock:
            cursor = await self._conn.execute(sql, params)
            await self._conn.commit()
            return cursor.rowcount

    async def executemany(self, sql: str, param_list: list[tuple]) -> int:
        """Execute a batch. Returns rowcount."""
        if self._conn is None:
            raise DatabaseError("Connection not open", code="NO_CONNECTION")
        async with self._lock:
            cursor = await self._conn.executemany(sql, param_list)
            await self._conn.commit()
            return cursor.rowcount

    async def fetch_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """Fetch a single row as a dict."""
        if self._conn is None:
            raise DatabaseError("Connection not open", code="NO_CONNECTION")
        cursor = await self._conn.execute(sql, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        """Fetch all rows as a list of dicts."""
        if self._conn is None:
            raise DatabaseError("Connection not open", code="NO_CONNECTION")
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def fetch_value(self, sql: str, params: tuple = ()) -> Any:
        """Fetch a single value from a single row."""
        row = await self.fetch_one(sql, params)
        if row is None:
            return None
        return next(iter(row.values()))

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            self._initialized = False
            log.info("database_closed")

    async def verify_wal(self) -> bool:
        """Check WAL mode is active."""
        mode = await self.fetch_value("PRAGMA journal_mode")
        return str(mode).lower() == "wal"

    async def table_exists(self, table_name: str) -> bool:
        row = await self.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return row is not None

    async def list_tables(self) -> list[str]:
        rows = await self.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        return [r["name"] for r in rows]

    async def integrity_check(self) -> bool:
        """Run SQLite integrity check."""
        result = await self.fetch_value("PRAGMA integrity_check")
        return result == "ok"

    async def __aenter__(self) -> "Database":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()


# ── Module-level singleton ────────────────────────────────────────────────
_db_instance: Optional[Database] = None


async def get_db() -> Database:
    """Get the shared database instance, initializing if needed."""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
        await _db_instance.initialize()
    return _db_instance


async def close_db() -> None:
    """Close the shared database instance."""
    global _db_instance
    if _db_instance is not None:
        await _db_instance.close()
        _db_instance = None
