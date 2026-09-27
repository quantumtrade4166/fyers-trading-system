"""
JARVIS Phase 1 — Comprehensive Test Suite
==========================================
Tests all Phase 1 modules.

Run:
    G:\fyers_data_pipeline\.venv\Scripts\python.exe -m pytest jarvis/tests/ -v
    G:\fyers_data_pipeline\.venv\Scripts\python.exe jarvis/tests/test_phase1.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pytest
import sys
import tempfile
from pathlib import Path

# Encoding fix
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary database for testing."""
    db_path = str(tmp_path / "test_jarvis.db")
    os.environ["JARVIS_DB_PATH"] = db_path
    os.environ["JARVIS_KILL_SWITCH"] = str(tmp_path / "kill_switch.flag")
    return db_path


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset module singletons between tests."""
    import jarvis.memory.database as db_mod
    import jarvis.memory.command_ledger as ledger_mod
    import jarvis.security.kill_switch as ks_mod
    import jarvis.security.permissions as perm_mod
    db_mod._db_instance = None
    ledger_mod._ledger = None
    ks_mod._kill_switch = None
    perm_mod._permissions = None
    yield
    db_mod._db_instance = None
    ledger_mod._ledger = None
    ks_mod._kill_switch = None
    perm_mod._permissions = None


# ── Database Tests ────────────────────────────────────────────────────────
class TestDatabase:
    @pytest.mark.asyncio
    async def test_connection(self, tmp_db):
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        tables = await db.list_tables()
        assert "command_ledger" in tables
        assert "audit_trail" in tables
        assert "goals" in tables
        assert "tasks" in tables
        assert "decisions" in tables
        assert "timeline" in tables
        assert "app_registry" in tables
        assert "project_map" in tables
        assert "preferences" in tables
        assert "commitments" in tables
        await db.close()

    @pytest.mark.asyncio
    async def test_parameterized_query(self, tmp_db):
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        result = await db.fetch_value("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        assert result >= 10
        await db.close()

    @pytest.mark.asyncio
    async def test_no_string_interpolation(self, tmp_db):
        """Verify that numeric params are stored correctly (not as strings)."""
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        await db.execute("INSERT INTO goals (goal_id, title, description, priority, status) VALUES (?, ?, ?, ?, ?)",
                         ("test_goal_1", "Test Goal", "desc", 3, "planning"))
        row = await db.fetch_one("SELECT * FROM goals WHERE goal_id = ?", ("test_goal_1",))
        assert row["priority"] == 3
        await db.close()

    @pytest.mark.asyncio
    async def test_wal_mode(self, tmp_db):
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        wal = await db.verify_wal()
        assert wal is True
        await db.close()

    @pytest.mark.asyncio
    async def test_integrity_check(self, tmp_db):
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        ok = await db.integrity_check()
        assert ok is True
        await db.close()


# ── Command Ledger Tests ──────────────────────────────────────────────────
class TestCommandLedger:
    @pytest.mark.asyncio
    async def test_record_command(self, tmp_db):
        from jarvis.memory.command_ledger import get_ledger
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        ledger = get_ledger()
        ledger._db = db

        record = await ledger.record("test command", detected_intent="test")
        assert record["command_id"] is not None
        assert record["original_command"] == "test command"
        assert record["detected_intent"] == "test"

        # Verify in DB
        row = await db.fetch_one("SELECT * FROM command_ledger WHERE command_id = ?", (record["command_id"],))
        assert row is not None
        assert row["original_command"] == "test command"
        await db.close()

    @pytest.mark.asyncio
    async def test_immutable_ledger(self, tmp_db):
        from jarvis.memory.command_ledger import get_ledger
        from jarvis.memory.database import Database
        from jarvis.exceptions import ImmutableLedgerError
        db = Database(tmp_db)
        await db.initialize()
        ledger = get_ledger()
        ledger._db = db

        record = await ledger.record("immutable test")
        cmd_id = record["command_id"]

        with pytest.raises(ImmutableLedgerError):
            await ledger.update(cmd_id, original_command="modified")
        await db.close()

    @pytest.mark.asyncio
    async def test_query_by_project(self, tmp_db):
        from jarvis.memory.command_ledger import get_ledger
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        ledger = get_ledger()
        ledger._db = db

        await ledger.record("project a cmd", target_project="proj_a")
        await ledger.record("project b cmd", target_project="proj_b")
        await ledger.record("project a cmd 2", target_project="proj_a")

        proj_a = await ledger.get_all(project="proj_a")
        assert len(proj_a) == 2
        assert all(c["target_project"] == "proj_a" for c in proj_a)
        await db.close()


# ── Goals Tests ───────────────────────────────────────────────────────────
class TestGoals:
    @pytest.mark.asyncio
    async def test_create_goal(self, tmp_db):
        from jarvis.memory.goals import GoalsManager
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        mgr = GoalsManager(db)

        goal = await mgr.goals.create("Test Goal", description="A test", priority=3)
        assert goal["goal_id"] is not None
        assert goal["title"] == "Test Goal"
        assert goal["status"] == "planning"
        assert goal["priority"] == 3
        await db.close()

    @pytest.mark.asyncio
    async def test_complete_goal(self, tmp_db):
        from jarvis.memory.goals import GoalsManager
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        mgr = GoalsManager(db)

        goal = await mgr.goals.create("Completable Goal")
        completed = await mgr.goals.complete(goal["goal_id"])
        assert completed["status"] == "completed"
        assert completed["progress_pct"] == 100
        await db.close()

    @pytest.mark.asyncio
    async def test_milestones(self, tmp_db):
        from jarvis.memory.goals import GoalsManager
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        mgr = GoalsManager(db)

        goal = await mgr.goals.create("Goal with milestones")
        ms = await mgr.milestones.create(goal["goal_id"], "First milestone", due_date="2025-12-31")
        assert ms["milestone_id"] is not None
        assert ms["status"] == "pending"

        completed_ms = await mgr.milestones.complete(ms["milestone_id"])
        assert completed_ms["status"] == "completed"
        await db.close()


# ── Tasks Tests ───────────────────────────────────────────────────────────
class TestTasks:
    @pytest.mark.asyncio
    async def test_create_task(self, tmp_db):
        from jarvis.memory.tasks import TasksManager
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        mgr = TasksManager(db)

        task = await mgr.tasks.create("Test Task", description="desc", priority=2)
        assert task["task_id"] is not None
        assert task["title"] == "Test Task"
        assert task["state"] == "pending"
        await db.close()

    @pytest.mark.asyncio
    async def test_task_state_transitions(self, tmp_db):
        from jarvis.memory.tasks import TasksManager, TaskState
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        mgr = TasksManager(db)

        task = await mgr.tasks.create("State task")
        await mgr.tasks.set_state(task["task_id"], TaskState.ACTIVE)
        updated = await mgr.tasks.get(task["task_id"])
        assert updated["state"] == TaskState.ACTIVE

        await mgr.tasks.set_state(task["task_id"], TaskState.COMPLETED)
        updated = await mgr.tasks.get(task["task_id"])
        assert updated["state"] == TaskState.COMPLETED
        assert updated["completed_at"] is not None
        await db.close()

    @pytest.mark.asyncio
    async def test_dependency_cycle_detection(self, tmp_db):
        from jarvis.memory.tasks import TasksManager
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        mgr = TasksManager(db)

        t1 = await mgr.tasks.create("Task 1")
        t2 = await mgr.tasks.create("Task 2")
        t3 = await mgr.tasks.create("Task 3")

        # Valid: t1 -> t2 -> t3
        assert await mgr.tasks.add_dependency(t1["task_id"], t2["task_id"]) is True
        assert await mgr.tasks.add_dependency(t2["task_id"], t3["task_id"]) is True

        # Cycle: t3 -> t1 would create t1 -> t2 -> t3 -> t1
        assert await mgr.tasks.add_dependency(t3["task_id"], t1["task_id"]) is False

        # Self-dependency
        assert await mgr.tasks.add_dependency(t1["task_id"], t1["task_id"]) is False
        await db.close()


# ── Decisions Tests ───────────────────────────────────────────────────────
class TestDecisions:
    @pytest.mark.asyncio
    async def test_create_decision(self, tmp_db):
        from jarvis.memory.decisions import Decisions
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        dec = Decisions(db)

        decision = await dec.create(
            "Strategy Choice",
            "Use DualMom for equities",
            reasoning="Better risk-adjusted returns",
            project="trading",
        )
        assert decision["decision_id"] is not None
        assert decision["decision"] == "Use DualMom for equities"
        await db.close()

    @pytest.mark.asyncio
    async def test_search_decisions(self, tmp_db):
        from jarvis.memory.decisions import Decisions
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        dec = Decisions(db)

        await dec.create("Python Choice", "Use Python for backtesting", project="code")
        await dec.create("Language Choice", "Use TypeScript for frontend", project="code")

        results = await dec.search("Python")
        assert len(results) == 1
        assert results[0]["title"] == "Python Choice"
        await db.close()


# ── Audit Trail Tests ─────────────────────────────────────────────────────
class TestAuditTrail:
    @pytest.mark.asyncio
    async def test_record_audit(self, tmp_db):
        from jarvis.security.audit import AuditTrail
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        audit = AuditTrail(db)

        entry = await audit.record("test_actor", "test_action", result="success")
        assert entry["entry_id"] is not None
        assert entry["actor"] == "test_actor"
        assert entry["action"] == "test_action"
        await db.close()

    @pytest.mark.asyncio
    async def test_chain_verification(self, tmp_db):
        from jarvis.security.audit import AuditTrail
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        audit = AuditTrail(db)

        for i in range(10):
            await audit.record(f"actor_{i}", f"action_{i}")

        verification = await audit.verify_chain()
        assert verification["total_entries"] == 10
        assert verification["valid"] is True
        await db.close()

    @pytest.mark.asyncio
    async def test_immutable_audit(self, tmp_db):
        from jarvis.security.audit import AuditTrail
        from jarvis.exceptions import ImmutableLedgerError
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        audit = AuditTrail(db)

        entry = await audit.record("test", "test")
        with pytest.raises(ImmutableLedgerError):
            await audit.update(entry["entry_id"], result="modified")
        await db.close()


# ── Permissions Tests ─────────────────────────────────────────────────────
class TestPermissions:
    def test_l0_auto_approved(self):
        from jarvis.security.permissions import Permissions, PermissionLevel
        perms = Permissions()
        result = perms.check("status", "some_app")
        assert result.granted is True
        assert result.level == PermissionLevel.L0_READ

    def test_l1_authorized_app(self):
        from jarvis.security.permissions import Permissions
        perms = Permissions()
        perms.authorize_app("VS Code")
        result = perms.check("launch", "VS Code")
        assert result.granted is True

    def test_l1_unauthorized_app(self):
        from jarvis.security.permissions import Permissions
        perms = Permissions()
        result = perms.check("launch", "Unknown App")
        assert result.granted is False

    def test_human_override(self):
        from jarvis.security.permissions import Permissions, PermissionLevel
        perms = Permissions()
        result = perms.check("execute", "rm -rf /", human_override=True)
        assert result.granted is True

    def test_temporary_permission(self):
        from jarvis.security.permissions import Permissions
        perms = Permissions()
        # First check: requires confirmation
        result = perms.check("execute", "myapp")
        assert result.requires_confirmation is True
        # Grant temporary permission
        perms.grant_temporary("execute:myapp", 1, ttl_seconds=3600)
        # Second check: auto-approved
        result = perms.check("execute", "myapp")
        assert result.granted is True
        assert result.requires_confirmation is False


# ── Kill Switch Tests ─────────────────────────────────────────────────────
class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_activation(self, tmp_db, tmp_path):
        from jarvis.security.kill_switch import KillSwitch
        ks = KillSwitch(flag_path=str(tmp_path / "kill.flag"))
        assert ks.is_active is False
        ks.activate()
        assert ks.is_active is True
        assert (tmp_path / "kill.flag").exists()
        ks.deactivate()
        assert ks.is_active is False

    @pytest.mark.asyncio
    async def test_kill_switch_blocks(self, tmp_db, tmp_path):
        from jarvis.security.kill_switch import KillSwitch
        from jarvis.exceptions import KillSwitchActiveError
        ks = KillSwitch(flag_path=str(tmp_path / "kill.flag"))
        ks.activate()
        with pytest.raises(KillSwitchActiveError):
            ks.check("launch")
        ks.deactivate()
        # L0 should still work when killed (but our check blocks all - correct per design)


# ── App Registry Tests ────────────────────────────────────────────────────
class TestAppRegistry:
    @pytest.mark.asyncio
    async def test_register_app(self, tmp_db):
        from jarvis.desktop.app_registry import AppRegistry
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        reg = AppRegistry(db)

        app = await reg.register("TestApp", aliases=["ta"], permission_level=1)
        assert app is not None
        retrieved = await reg.get("TestApp")
        assert retrieved is not None
        assert retrieved["name"] == "TestApp"
        await db.close()

    @pytest.mark.asyncio
    async def test_resolve_alias(self, tmp_db):
        from jarvis.desktop.app_registry import AppRegistry
        from jarvis.memory.database import Database
        db = Database(tmp_db)
        await db.initialize()
        reg = AppRegistry(db)

        await reg.register("VS Code", aliases=["vscode", "code"])
        results = await reg.resolve("vscode")
        assert len(results) == 1
        assert results[0]["name"] == "VS Code"
        await db.close()


# ── Integration Test ──────────────────────────────────────────────────────
class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_flow(self, tmp_db):
        """Test a full JARVIS workflow: init -> create goal -> create task -> record decision."""
        from jarvis.memory.database import Database
        from jarvis.memory.goals import GoalsManager
        from jarvis.memory.tasks import TasksManager
        from jarvis.memory.decisions import Decisions
        from jarvis.memory.timeline import Timeline
        from jarvis.security.audit import AuditTrail
        from jarvis.desktop.app_registry import AppRegistry
        from jarvis.security.permissions import get_permissions
        from jarvis.security.kill_switch import get_kill_switch

        db = Database(tmp_db)
        await db.initialize()

        # Seed
        get_kill_switch()
        get_permissions()

        # Create goal
        goals_mgr = GoalsManager(db)
        goal = await goals_mgr.goals.create("Full Flow Goal", priority=1)

        # Create task linked to goal
        tasks_mgr = TasksManager(db)
        task = await tasks_mgr.tasks.create("Full Flow Task", goal_id=goal["goal_id"], priority=2)

        # Complete task
        await tasks_mgr.tasks.set_state(task["task_id"], "completed")

        # Record decision
        dec = Decisions(db)
        await dec.create("Flow Decision", "We chose this path", project="test")

        # Timeline event
        timeline = Timeline(db)
        await timeline.add("goal_created", "Goal Created", project="test", summary=goal["title"])

        # Audit entry
        audit = AuditTrail(db)
        entry = await audit.record("test", "full_flow_test", result="success")

        # Verify
        g = await goals_mgr.goals.get(goal["goal_id"])
        assert g["status"] == "planning"  # not completed yet

        t = await tasks_mgr.tasks.get(task["task_id"])
        assert t["state"] == "completed"

        events = await timeline.get(project="test")
        assert len(events) == 1

        v = await audit.verify_chain()
        assert v["total_entries"] >= 1

        await db.close()
