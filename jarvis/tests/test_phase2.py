"""
JARVIS — Phase 2 Test Suite
============================
Tests for:
  - Natural language parser
  - Agent loop (health checks)
  - Desktop controller (real win32gui actions)
  - Integration tests (CLI end-to-end)

All tests run without requiring a running VPS or live trading.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

# Ensure project root in path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


# ── NLParser Tests ───────────────────────────────────────────────────────────

class TestNLParser:
    """Tests for the natural language command parser."""

    def setup_method(self):
        from jarvis.nlp_parser import NLParser
        self.parser = NLParser()

    def test_launch_app(self):
        r = self.parser.parse("launch VS Code")
        assert r.intent == "launch"
        assert r.action == "launch"
        assert r.target == "VS Code"
        assert r.confidence > 0.8

    def test_launch_with_dir(self):
        r = self.parser.parse("launch VS Code in G:\\fyers_data_pipeline")
        assert r.intent == "launch"
        assert r.target == "VS Code"
        assert r.working_dir

    def test_open_alias(self):
        r = self.parser.parse("open Chrome")
        assert r.intent == "launch"
        assert r.target == "Chrome"

    def test_focus_app(self):
        r = self.parser.parse("focus Obsidian")
        assert r.intent == "focus"
        assert r.action == "focus"
        assert r.target == "Obsidian"

    def test_close_app(self):
        r = self.parser.parse("close Chrome")
        assert r.intent == "close"
        assert r.action == "close"
        assert r.target == "Chrome"

    def test_data_freshness(self):
        r = self.parser.parse("check data freshness")
        assert r.intent == "data_freshness"
        assert r.confidence > 0.7

    def test_data_update(self):
        r = self.parser.parse("update the data")
        assert r.intent == "data_update"
        assert r.confidence > 0.7

    def test_kill_switch(self):
        r = self.parser.parse("stop everything")
        assert r.intent == "kill_switch"
        assert r.confidence > 0.9

    def test_emergency_alias(self):
        r = self.parser.parse("emergency stop")
        assert r.intent == "kill_switch"

    def test_create_goal(self):
        r = self.parser.parse("create a goal to fix backtest engine")
        assert r.intent == "create_goal"
        assert "fix backtest engine" in r.target

    def test_list_goals(self):
        r = self.parser.parse("list my goals")
        assert r.intent == "list_goals"

    def test_list_tasks(self):
        r = self.parser.parse("show my tasks")
        assert r.intent == "list_tasks"

    def test_vps_status(self):
        r = self.parser.parse("what is the status of VPS")
        assert r.intent == "status"
        assert r.target == "VPS"

    def test_monitor_vps(self):
        r = self.parser.parse("monitor VPS")
        assert r.intent == "agent_monitoring"

    def test_dualmom_agent(self):
        r = self.parser.parse("dualmom status")
        assert r.intent == "agent_dualmom"

    def test_fyers_agent(self):
        r = self.parser.parse("fyers check freshness")
        assert r.intent == "agent_fyers"

    def test_help(self):
        r = self.parser.parse("help")
        assert r.intent == "help"
        assert r.confidence > 0.9

    def test_help_question_mark(self):
        r = self.parser.parse("?")
        assert r.intent == "help"

    def test_unknown_fallback(self):
        r = self.parser.parse("do the thing with the stuff")
        assert r.fallback is True
        assert r.intent == "create_goal"

    def test_empty_command(self):
        r = self.parser.parse("")
        assert r.intent == "unknown"
        assert r.confidence == 0.0

    def test_app_name_resolution(self):
        """Aliases should resolve to canonical names."""
        assert self.parser._resolve_app_name("vscode") == "VS Code"
        assert self.parser._resolve_app_name("claude") == "Claude Code"
        assert self.parser._resolve_app_name("terminal") == "Windows Terminal"
        assert self.parser._resolve_app_name("obsidian") == "Obsidian"
        assert self.parser._resolve_app_name("VS Code") == "VS Code"  # exact match

    def test_deadline_extraction_weekday(self):
        deadline = self.parser._extract_deadline("by Friday")
        assert "friday" in deadline.lower()

    def test_deadline_extraction_date(self):
        deadline = self.parser._extract_deadline("by 2026-10-01")
        assert "2026-10-01" in deadline

    def test_deadline_extraction_relative(self):
        deadline = self.parser._extract_deadline("by tomorrow")
        assert "tomorrow" in deadline.lower()


# ── Desktop Controller Tests ─────────────────────────────────────────────────

class TestDesktopController:
    """Tests for the desktop controller (requires Windows)."""

    @pytest.mark.asyncio
    async def test_launch_notepad(self):
        from jarvis.desktop.remote_control import DesktopController
        from jarvis.memory.database import Database
        from jarvis.desktop.app_registry import AppRegistry, seed_default_apps
        db = Database()
        await db.initialize()

        # Ensure all default apps + Notepad are registered
        reg = AppRegistry(db)
        await seed_default_apps(reg)
        notepad_app = {
            "name": "Notepad",
            "aliases": ["notepad", "notepad.exe"],
            "exe_path": "notepad.exe",
            "permission_level": 1,
            "allowed_actions": ["launch", "focus", "close", "status", "switch"],
            "cli_automation": True,
        }
        existing = await reg.get("Notepad")
        if not existing:
            await reg.register(**notepad_app)

        # Authorize Notepad
        from jarvis.security.permissions import get_permissions
        perms = get_permissions()
        perms.authorize_app("Notepad")

        ctrl = DesktopController(db)
        await ctrl.initialize()
        result = await ctrl.execute("launch", "notepad")
        assert result["status"] in ("running", "error")
        if result["status"] == "running":
            assert "pid" in result

    @pytest.mark.asyncio
    async def test_status_not_running(self):
        from jarvis.desktop.remote_control import DesktopController
        from jarvis.memory.database import Database
        db = Database()
        await db.initialize()
        ctrl = DesktopController(db)
        await ctrl.initialize()

        result = await ctrl.execute("status", "nonexistent_app_xyz")
        assert "status" in result

    @pytest.mark.asyncio
    async def test_find_window_util(self):
        from jarvis.desktop.remote_control import DesktopController
        from jarvis.memory.database import Database
        db = Database()
        await db.initialize()
        ctrl = DesktopController(db)
        await ctrl.initialize()

        hwnd = ctrl._find_window("notepad")
        assert hwnd == 0 or isinstance(hwnd, int)

    @pytest.mark.asyncio
    async def test_kill_switch_blocks_launch(self):
        from jarvis.desktop.remote_control import DesktopController
        from jarvis.memory.database import Database
        from jarvis.security.kill_switch import get_kill_switch
        db = Database()
        await db.initialize()
        ctrl = DesktopController(db)
        await ctrl.initialize()

        ks = get_kill_switch()
        ks.activate()
        try:
            with pytest.raises(Exception):
                await ctrl.execute("launch", "notepad")
        finally:
            ks.deactivate()

    @pytest.mark.asyncio
    async def test_permission_denied_unknown_app(self):
        from jarvis.desktop.remote_control import DesktopController
        from jarvis.memory.database import Database
        db = Database()
        await db.initialize()
        ctrl = DesktopController(db)
        await ctrl.initialize()

        with pytest.raises(Exception):
            await ctrl.execute("launch", "totally_unknown_app_xyz")


# ── Agent Loop Tests ─────────────────────────────────────────────────────────

class TestAgentLoop:
    """Tests for the agent monitoring loop."""

    def test_agent_loop_instantiation(self):
        from jarvis.agent_loop import AgentLoop
        loop = AgentLoop(enabled=True)
        assert loop.is_running is False
        assert loop.get_last_results() == {}

    @pytest.mark.asyncio
    async def test_agent_loop_lifecycle(self):
        from jarvis.agent_loop import AgentLoop
        loop = AgentLoop(enabled=True)
        await loop.start()
        assert loop.is_running is True
        await loop.stop()
        assert loop.is_running is False

    @pytest.mark.asyncio
    async def test_agent_loop_disabled(self):
        from jarvis.agent_loop import AgentLoop
        loop = AgentLoop(enabled=False)
        await loop.start()
        assert loop.is_running is False  # should not start when disabled
        await loop.stop()

    @pytest.mark.asyncio
    async def test_custom_check(self):
        from jarvis.agent_loop import AgentLoop, CheckResult
        loop = AgentLoop(enabled=True)

        def my_check():
            return CheckResult("test_check", "healthy", "all good", {"value": 42})

        loop.register_check("test_check", my_check)
        # Run the check directly (bypass scheduler timing)
        await loop._run_check("test_check", my_check)

        results = loop.get_last_results()
        assert "test_check" in results
        assert results["test_check"]["status"] == "healthy"
        assert results["test_check"]["data"]["value"] == 42

    @pytest.mark.asyncio
    async def test_disk_check_runs(self):
        from jarvis.agent_loop import AgentLoop
        loop = AgentLoop(enabled=True)
        check_fn = loop._check_disk_space
        # Run the check directly (bypass scheduler timing)
        await loop._run_check("disk_space", check_fn)

        results = loop.get_last_results()
        assert "disk_space" in results
        assert results["disk_space"]["status"] in ("healthy", "degraded", "failed")
        assert "free_gb" in results["disk_space"]["data"]


# ── Integration Tests ────────────────────────────────────────────────────────

class TestIntegration:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_full_flow_setup_then_status(self):
        """Setup should succeed and status should reflect it."""
        from jarvis.main import JARVIS
        jarvis = JARVIS()
        init = await jarvis.process("setup")
        assert init["status"] in ("initialized", "already_initialized")

        status = await jarvis.process("status")
        assert status["status"] == "ok"
        assert status["system"] == "JARVIS"

    @pytest.mark.asyncio
    async def test_nl_parser_integration(self):
        """NLParser should feed into JARVIS correctly."""
        from jarvis.nlp_parser import NLParser
        p = NLParser()

        # Verify parser output maps to expected handlers
        r = p.parse("launch VS Code")
        assert r.intent == "launch"
        assert r.action == "launch"
        assert r.target

        r = p.parse("stop everything")
        assert r.intent == "kill_switch"

        r = p.parse("create a goal to add OI tracking")
        assert r.intent == "create_goal"

    @pytest.mark.asyncio
    async def test_agent_loop_vps_check(self):
        """VPS check should attempt connection and return a result."""
        from jarvis.agent_loop import AgentLoop
        loop = AgentLoop(enabled=True)
        result = loop._check_vps()
        assert result.name == "vps_ping"
        assert result.status in ("healthy", "failed", "degraded")
        assert "vps_ip" in result.data
