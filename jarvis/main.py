"""
JARVIS — Main Orchestrator
==========================
Entry point for the JARVIS system.

Architecture:
  1. Parse natural language command
  2. Detect intent and target
  3. Route to appropriate handler:
     - Memory queries → memory layer
     - Desktop control → desktop controller
     - Agent commands → specialized agent
     - Setup commands → setup module
  4. Record everything in the command ledger
  5. Return structured result

Usage:
    python -m jarvis.main "status"
    python -m jarvis.main "launch VS Code"
    python -m jarvis.main "init"  # Initialize the system
    python -m jarvis.main "desktop status"
    python -m jarvis.main "memory list goals"
    python -m jarvis.main "agent dualmom status"
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Ensure the project root is in the path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Encoding fix
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from jarvis.memory.command_ledger import get_ledger
from jarvis.memory.decisions import Decisions
from jarvis.memory.goals import GoalsManager
from jarvis.memory.tasks import TasksManager
from jarvis.agent_loop import AgentLoop
from jarvis.logging_config import get_logger, set_trace_id
from jarvis.config import get_config

log = get_logger("main")


def generate_trace_id() -> str:
    from datetime import datetime, timezone
    import uuid
    return f"tr_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


class JARVIS:
    """
    Main JARVIS orchestrator.

    Routes commands to the appropriate subsystem:
      - setup: Initialize the system
      - desktop: Desktop control commands
      - memory: Memory/goals/tasks commands
      - agent: Specialized agent commands
      - config: Configuration commands
      - status: System status
    """

    def __init__(self, agent_loop_enabled: bool = True) -> None:
        self.config = get_config()
        self._initialized = False
        self._agent_loop_enabled = agent_loop_enabled
        self._agent_loop: AgentLoop | None = None

    async def initialize(self) -> dict:
        """Initialize all subsystems."""
        if self._initialized:
            return {"status": "already_initialized"}

        from jarvis.memory.database import Database, get_db
        from jarvis.security.audit import AuditTrail
        from jarvis.security.permissions import get_permissions
        from jarvis.security.kill_switch import get_kill_switch
        from jarvis.desktop.remote_control import DesktopController

        # Initialize database
        self.db = Database()
        await self.db.initialize()

        # Initialize subsystems
        self._ledger = get_ledger()
        self._audit = AuditTrail(self.db)
        self._goals = GoalsManager(self.db)
        self._tasks = TasksManager(self.db)
        self._decisions = Decisions(self.db)
        self._perms = get_permissions()
        self._kill = get_kill_switch()
        self._desktop = DesktopController(self.db)

        # Seed defaults
        init_result = await self._desktop.initialize()

        # Start agent loop
        if self._agent_loop_enabled:
            await self._start_agent_loop()

        self._initialized = True
        log.info("jarvis_initialized")
        return {
            "status": "initialized",
            "database": True,
            "desktop": init_result,
            "agent_loop": self._agent_loop.is_running if self._agent_loop else False,
        }

    async def process(self, command: str, *, human_override: bool = False) -> dict:
        """
        Process a natural language command.

        Args:
            command: The user's command string.
            human_override: If True, the user explicitly requested this action.

        Returns:
            Result dict with status and response.
        """
        if not self._initialized:
            await self.initialize()

        trace_id = generate_trace_id()
        set_trace_id(trace_id)

        log.info("command_received", command=command, trace_id=trace_id)

        # Parse command
        parts = command.strip().lower().split(maxsplit=1)
        primary = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        args_original = command[len(primary):].strip()

        # Route to appropriate handler
        try:
            if primary in ("setup", "init", "initialize"):
                result = await self._handle_setup()
            elif primary in ("desktop", "launch", "open", "close", "focus", "run"):
                result = await self._handle_desktop(primary, args_original, human_override)
            elif primary in ("memory", "goals", "tasks", "decisions"):
                result = await self._handle_memory(primary, args_original)
            elif primary in ("agent", "dualmom", "fyers", "monitoring"):
                result = await self._handle_agent(primary, args_original)
            elif primary in ("status", "health", "check"):
                result = await self._handle_status()
            elif primary in ("config"):
                result = await self._handle_config(args_original)
            elif primary in ("help", "?", "commands"):
                result = await self._handle_help()
            elif primary in ("permissions"):
                result = await self._handle_permissions(args_original)
            elif primary in ("kill_switch", "emergency", "stop_everything"):
                result = await self._handle_kill_switch()
            else:
                # Default: treat as a goal creation or task
                result = await self._handle_default(command)

        except Exception as e:
            log.error("command_failed", error=str(e), trace_id=trace_id)
            result = {"status": "error", "error": str(e), "trace_id": trace_id}

        # Record in ledger
        try:
            await self._ledger.record(
                command,
                detected_intent=primary,
                result=json.dumps(result),
            )
        except Exception:
            pass

        log.info("command_complete", primary=primary, status=result.get("status", "unknown"),
                 trace_id=trace_id)
        return result

    # ── Handlers ──────────────────────────────────────────────────────────

    async def _handle_setup(self) -> dict:
        """Initialize the JARVIS system."""
        return await self.initialize()

    async def _handle_desktop(
        self, primary: str, args: str, human_override: bool
    ) -> dict:
        """Handle desktop control commands."""
        action_map = {
            "launch": "launch",
            "open": "launch",
            "run": "launch",
            "close": "close",
            "focus": "focus",
        }
        action = action_map.get(primary, primary)
        target = args.strip()
        if not target:
            return {"status": "error", "error": "Target required (app name or path)"}
        result = await self._desktop.execute(
            action, target, human_override=human_override
        )
        return result

    async def _handle_memory(self, primary: str, args: str) -> dict:
        """Handle memory queries."""
        parts = args.strip().split(maxsplit=1) if args.strip() else []
        sub = parts[0] if parts else "list"
        sub_args = parts[1] if len(parts) > 1 else ""

        if primary in ("memory", "goals"):
            if sub == "list":
                goals = await self._goals.goals.get_all()
                return {
                    "status": "ok",
                    "type": "goals",
                    "count": len(goals),
                    "items": [
                        {
                            "id": g["goal_id"],
                            "title": g["title"],
                            "status": g["status"],
                            "progress": g.get("progress_pct", 0),
                        }
                        for g in goals
                    ],
                }
            elif sub == "create":
                goal = await self._goals.goals.create(sub_args)
                return {"status": "ok", "type": "goal_created", "goal": goal}
            else:
                # Try to get a specific goal
                goal = await self._goals.goals.get(sub_args)
                if goal:
                    return {"status": "ok", "type": "goal", "goal": goal}
                return {"status": "error", "error": f"Goal '{sub_args}' not found"}

        elif primary == "tasks":
            if sub == "list":
                tasks = await self._tasks.tasks.get_all()
                return {
                    "status": "ok",
                    "type": "tasks",
                    "count": len(tasks),
                    "items": [
                        {
                            "id": t["task_id"],
                            "title": t["title"],
                            "state": t["state"],
                            "project": t.get("project"),
                        }
                        for t in tasks
                    ],
                }
            elif sub == "create":
                task = await self._tasks.tasks.create(sub_args)
                return {"status": "ok", "type": "task_created", "task": task}
            else:
                task = await self._tasks.tasks.get(sub_args)
                if task:
                    return {"status": "ok", "type": "task", "task": task}
                return {"status": "error", "error": f"Task '{sub_args}' not found"}

        elif primary == "decisions":
            decisions = await self._decisions.get_for_project(sub_args or "default")
            return {
                "status": "ok",
                "type": "decisions",
                "count": len(decisions),
                "items": decisions[:20],
            }

        return {"status": "error", "error": f"Unknown memory command: {primary} {args}"}

    async def _handle_agent(self, primary: str, args: str) -> dict:
        """Handle agent commands."""
        agent_name = primary.replace("agent:", "").strip()

        # Lazy import agents
        if agent_name == "dualmom":
            from jarvis.agent.dualmom_agent import DualMomAgent
            agent = DualMomAgent(self.db)
        elif agent_name == "fyers":
            from jarvis.agent.fyers_agent import FyersAgent
            agent = FyersAgent(self.db)
        elif agent_name == "monitoring":
            from jarvis.agent.monitoring_agent import MonitoringAgent
            agent = MonitoringAgent(self.db)
        else:
            return {
                "status": "error",
                "error": f"Unknown agent: {agent_name}",
                "available": ["dualmom", "fyers", "monitoring"],
            }

        return await agent.run(args)

    async def _handle_status(self) -> dict:
        """System status."""
        db_status = {}
        try:
            from jarvis.memory.database import get_db
            db = await get_db()
            tables = await db.list_tables()
            db_status = {
                "database": "connected",
                "tables": tables,
                "wal_mode": await db.verify_wal(),
            }
        except Exception as e:
            db_status = {"database": "error", "error": str(e)}

        return {
            "status": "ok",
            "system": "JARVIS",
            "initialized": self._initialized,
            "agent_loop": self.get_agent_loop_status(),
            "database": db_status,
            "permissions": {
                "authorized_apps": list(self._perms._authorized_apps) if hasattr(self._perms, '_authorized_apps') else [],
            },
            "kill_switch": self._kill.is_active,
        }

    async def _handle_config(self, args: str) -> dict:
        """Configuration commands."""
        if not args:
            cfg = self.config
            return {
                "status": "ok",
                "config": {
                    "db_path": cfg.db_path,
                    "wal_mode": cfg.db_wal_mode,
                    "desktop_enabled": cfg.desktop_enabled,
                    "gui_automation": cfg.gui_automation_enabled,
                },
            }
        return {"status": "ok", "message": "Config read-only in Phase 1"}

    async def _handle_help(self) -> dict:
        """Show available commands."""
        return {
            "status": "ok",
            "commands": {
                "setup": "Initialize JARVIS system",
                "desktop launch <app>": "Launch an application",
                "desktop status <app>": "Check app status",
                "desktop open <file/folder>": "Open a file or folder",
                "memory goals list": "List all goals",
                "memory goals create <title>": "Create a new goal",
                "memory tasks list": "List all tasks",
                "memory tasks create <title>": "Create a new task",
                "memory decisions [project]": "List decisions for a project",
                "agent dualmom <command>": "DualMom agent commands",
                "agent fyers <command>": "Fyers agent commands",
                "agent monitoring <command>": "Monitoring agent commands",
                "status": "System status",
                "config": "View configuration",
                "permissions": "View permission settings",
                "kill_switch": "Activate emergency stop",
                "help": "Show this help",
            },
        }

    async def _handle_permissions(self, args: str) -> dict:
        """Permission commands."""
        parts = args.strip().split()
        if parts and parts[0] == "authorize":
            app = parts[1] if len(parts) > 1 else ""
            if app:
                self._perms.authorize_app(app)
                return {"status": "ok", "message": f"Authorized: {app}"}
        return {
            "status": "ok",
            "authorized_apps": list(self._perms._authorized_apps),
        }

    async def _handle_kill_switch(self) -> dict:
        """Activate kill switch."""
        self._kill.activate()
        return {"status": "kill_switch_activated", "message": "All non-read operations blocked"}

    async def _handle_default(self, command: str) -> dict:
        """
        Default handler: if the command doesn't match any other handler,
        try to interpret it as a goal creation.
        """
        goal = await self._goals.goals.create(
            command,
            description="Created from unclassified command",
        )
        return {
            "status": "ok",
            "type": "goal_created",
            "message": "Command interpreted as a new goal",
            "goal": goal,
        }


    # ── Agent Loop ────────────────────────────────────────────────────────

    async def _start_agent_loop(self) -> None:
        """Start the background agent monitoring loop."""
        from jarvis.memory.timeline import Timeline
        from jarvis.security.audit import AuditTrail
        try:
            self._agent_loop = AgentLoop(
                timeline=Timeline(self.db),
                audit=AuditTrail(self.db),
                enabled=True,
            )
            await self._agent_loop.start()
            log.info("agent_loop_started_from_main")
        except Exception as e:
            log.warning("agent_loop_failed_to_start", error=str(e))
            self._agent_loop = None

    @property
    def agent_loop_running(self) -> bool:
        """Whether the agent loop is currently running."""
        return self._agent_loop is not None and self._agent_loop.is_running

    def get_agent_loop_status(self) -> dict:
        """Return agent loop health data for status endpoint."""
        if not self._agent_loop:
            return {"running": False, "message": "Agent loop not started"}
        results = self._agent_loop.get_last_results()
        return {
            "running": self._agent_loop.is_running,
            "checks": len(results),
            "last_results": results,
        }


# ── CLI Entry Point ───────────────────────────────────────────────────────
def main() -> None:
    """CLI entry point."""
    command = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "help"
    human_override = "--confirm" in sys.argv or "--override" in sys.argv

    jarvis = JARVIS()
    result = asyncio.run(jarvis.process(command, human_override=human_override))

    print(json.dumps(result, indent=2, default=str))

    # Exit with appropriate code
    if result.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
