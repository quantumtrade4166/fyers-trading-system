"""
JARVIS Desktop — Remote Control
=================================
Controller for launching, focusing, and managing desktop applications.

Supports two modes:
  - CLI automation: apps with command-line interfaces (git, python, Claude Code, etc.)
  - GUI automation: apps that need keyboard/mouse control

Discovery:
  - Command registry checks known CLIs first, then resolves exe_path from registry
  - On first encounter, probes PATH for the executable

Rules:
  - Kill switch checked before every action.
  - Permissions checked before every action.
  - Audit trail recorded after every action.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .app_registry import AppRegistry, DEFAULT_APPS, seed_default_apps
from .project_map import Projects, ProjectMap, seed_default_projects
from ..exceptions import AppNotFoundError, KillSwitchActiveError, PermissionDeniedError
from ..constants import (
    AppStatus,
    HealthStatus,
    DesktopAction,
    PermissionLevel,
    PermissionDecision,
    KILL_SWITCH_FILE,
)
from ..logging_config import get_logger
from ..memory.database import Database, get_db
from ..security.audit import AuditTrail
from ..memory.command_ledger import get_ledger
from ..security.permissions import get_permissions
from ..security.kill_switch import get_kill_switch

log = get_logger("desktop_control")


class DesktopController:
    """
    Main desktop control engine.

    Orchestrates:
      1. Permission check
      2. Kill switch check
      3. App lookup (registry → PATH discovery)
      4. Action execution
      5. Audit trail record
    """

    def __init__(self, db: Optional[Database] = None) -> None:
        self._db = db
        self._apps = AppRegistry(db)
        self._projects = Projects(db)
        self._project_map = ProjectMap(db)
        self._audit = AuditTrail(db)
        self._ledger = get_ledger()
        self._perms = get_permissions()
        self._kill = get_kill_switch()

    async def _get_db(self) -> Database:
        if self._db is not None:
            return self._db
        return await get_db()

    # ── Initialization ────────────────────────────────────────────────────
    async def initialize(self) -> dict:
        """Initialize desktop control: seed apps, projects, authorize defaults."""
        # Initialize DB
        db = await self._get_db()
        await self._apps.__class__(db)._get_db() if hasattr(self._apps, '_get_db') else None
        await seed_default_apps(self._apps)
        await seed_default_projects()

        # Authorize default apps for L0-L1
        perms = get_permissions()
        for app_name in ["Claude Code", "VS Code", "Obsidian", "Windows Terminal",
                         "PowerShell", "Chrome", "File Explorer", "Git", "Python", "JARVIS"]:
            perms.authorize_app(app_name)

        result = {
            "status": "initialized",
            "registered_apps": len(await self._apps.list_all()),
            "registered_projects": len(await self._projects.list_all()),
        }
        log.info("desktop_initialized", **result)
        return result

    # ── Main Action Dispatcher ────────────────────────────────────────────
    async def execute(
        self,
        action: str,
        target: str = "",
        *,
        project: Optional[str] = None,
        working_dir: Optional[str] = None,
        args: Optional[str] = None,
        human_override: bool = False,
    ) -> dict:
        """
        Execute a desktop action.

        Args:
            action: The action to perform (launch, focus, close, etc.)
            target: The target (app name, file path, URL, etc.)
            project: Optional project association.
            working_dir: Working directory for the action.
            args: Additional arguments.
            human_override: If True, user explicitly requested this.

        Returns:
            Result dict with status, output, errors.
        """
        # Record the command
        command_id = None
        try:
            cmd_record = await self._ledger.record(
                f"desktop:{action} {target}",
                detected_intent="desktop_control",
                target_project=project,
                tools_used="desktop_controller",
            )
            command_id = cmd_record["command_id"]
        except Exception:
            pass  # Non-critical

        # Check kill switch
        try:
            self._kill.check(action)
        except KillSwitchActiveError:
            await self._audit.record(
                actor="desktop_controller",
                action=action,
                command_id=command_id,
                target=target,
                result="blocked",
                error_info="Kill switch active",
            )
            raise

        # Check permissions
        perm = self._perms.check(action, target, human_override=human_override)
        if not perm.granted:
            raise PermissionDeniedError(
                f"Permission denied for '{action}' on '{target}': {perm.reason}",
                code="PERMISSION_DENIED",
            )

        # Dispatch
        start = time.time()
        result: dict[str, Any] = {"action": action, "target": target, "status": "unknown"}

        try:
            if action == DesktopAction.LAUNCH:
                result = await self._action_launch(target, working_dir=working_dir, args=args)
            elif action == DesktopAction.STATUS:
                result = await self._action_status(target)
            elif action == DesktopAction.FOCUS:
                result = await self._action_focus(target)
            elif action == DesktopAction.CLOSE:
                result = await self._action_close(target)
            elif action == DesktopAction.SWITCH:
                result = await self._action_switch(target)
            elif action == DesktopAction.OPEN_FILE:
                result = await self._action_open_file(target)
            elif action == DesktopAction.OPEN_FOLDER:
                result = await self._action_open_folder(target)
            elif action == DesktopAction.EXECUTE:
                result = await self._action_execute(target, working_dir=working_dir, args=args)
            else:
                result = {"status": "unknown_action", "action": action}

        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            await self._audit.record(
                actor="desktop_controller",
                action=action,
                command_id=command_id,
                target=target,
                result="error",
                error_info=str(e),
            )
            return result

        # Audit
        duration = int((time.time() - start) * 1000)
        await self._audit.record(
            actor="desktop_controller",
            action=action,
            command_id=command_id,
            target=target,
            result=result.get("status", "unknown"),
            duration_ms=duration,
        )

        return result

    # ── Action Implementations ────────────────────────────────────────────
    async def _action_launch(self, target: str, *, working_dir: str = "", args: str = "") -> dict:
        """Launch an application."""
        if not target:
            raise ValueError("Target (app name or path) required for launch")

        # Resolve via registry
        app = await self._apps.resolve(target)
        exe_path = ""
        if app:
            exe_path = app[0].get("exe_path", "")

        # Try PATH resolution
        exe_name = exe_path or target
        if not shutil.which(exe_name):
            # Common exe patterns
            for suffix in ["", ".exe", ".cmd", ".bat"]:
                if shutil.which(exe_name + suffix):
                    exe_name = exe_name + suffix
                    break

        if not shutil.which(exe_name) and not exe_path:
            raise AppNotFoundError(f"Could not find executable for '{target}'")

        full_path = exe_path or shutil.which(exe_name) or exe_name
        cwd = working_dir or os.getcwd()

        if args:
            cmd = [full_path] + args.split()
        else:
            cmd = [full_path]

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                creationflags=subprocess.DETACHED_PROCESS,
            )
            await self._apps.update_status(target, AppStatus.RUNNING)
            return {
                "status": AppStatus.RUNNING,
                "pid": proc.pid,
                "command": " ".join(cmd),
                "working_dir": cwd,
            }
        except OSError as e:
            raise AppNotFoundError(f"Failed to launch '{target}': {e}")

    async def _action_status(self, target: str) -> dict:
        """Check if an application is running."""
        app = await self._apps.resolve(target)
        if not app:
            return {"status": AppStatus.UNKNOWN, "target": target}
        return {
            "status": app[0].get("last_known_status", AppStatus.UNKNOWN),
            "health": app[0].get("health_status", HealthStatus.UNKNOWN),
            "name": app[0]["name"],
        }

    async def _action_focus(self, target: str) -> dict:
        """Focus an application window using win32gui."""
        try:
            import win32gui
            import win32con
            import win32process

            hwnd = self._find_window(target)
            if not hwnd:
                return {"status": "not_found", "target": target,
                        "message": f"No window found for '{target}'"}

            # Restore if minimized, then bring to front
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            title = win32gui.GetWindowText(hwnd)
            return {"status": "focused", "target": target, "window_title": title, "hwnd": hwnd}
        except ImportError:
            return {"status": "unavailable", "target": target,
                    "message": "pywin32 not installed — install pywin32 for GUI control"}
        except Exception as e:
            return {"status": "error", "target": target, "error": str(e)}

    async def _action_close(self, target: str) -> dict:
        """Close an application window using win32gui."""
        try:
            import win32gui
            import win32con

            hwnd = self._find_window(target)
            if not hwnd:
                return {"status": "not_found", "target": target,
                        "message": f"No window found for '{target}'"}

            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return {"status": "close_sent", "target": target, "hwnd": hwnd}
        except ImportError:
            return {"status": "unavailable", "target": target,
                    "message": "pywin32 not installed"}
        except Exception as e:
            return {"status": "error", "target": target, "error": str(e)}

    async def _action_switch(self, target: str) -> dict:
        """Switch to an application (alias for focus with alt+tab fallback)."""
        result = await self._action_focus(target)
        if result.get("status") == "not_found":
            # Try alt+tab to find the window
            try:
                import win32api
                import win32con
                win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                win32api.keybd_event(win32con.VK_TAB, 0, 0, 0)
                win32api.keybd_event(win32con.VK_TAB, 0, win32con.KEYEVENTF_KEYUP, 0)
                win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                result = {"status": "switched", "target": target,
                          "note": "Used Alt+Tab (window not found by name)"}
            except ImportError:
                pass
        return result

    def _find_window(self, target: str) -> int:
        """Find a window HWND by app name or title fragment."""
        import win32gui

        results: list[tuple[int, str]] = []

        def callback(hwnd: int, _extra: list) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            # Match by target (case-insensitive)
            if target.lower() in title.lower():
                _extra.append((hwnd, title))

        win32gui.EnumWindows(callback, results)
        if results:
            # Prefer exact title match, then longest match
            results.sort(key=lambda x: (x[1].lower() != target.lower(), -len(x[1])))
            return results[0][0]
        return 0

    async def _action_open_file(self, target: str) -> dict:
        """Open a file with its default application."""
        target_path = Path(target)
        if not target_path.exists():
            raise AppNotFoundError(f"File not found: {target}")
        os.startfile(str(target_path.absolute()))
        return {"status": "opened", "path": str(target_path.absolute())}

    async def _action_open_folder(self, target: str) -> dict:
        """Open a folder in File Explorer."""
        target_path = Path(target)
        if not target_path.exists():
            raise AppNotFoundError(f"Folder not found: {target}")
        os.startfile(str(target_path.absolute()))
        return {"status": "opened", "path": str(target_path.absolute())}

    async def _action_execute(
        self, target: str, *, working_dir: str = "", args: str = ""
    ) -> dict:
        """Execute a shell command (L2+ action)."""
        cmd_str = f"{target} {args or ''}".strip()
        cwd = working_dir or os.getcwd()
        try:
            result = subprocess.run(
                cmd_str,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {
                "status": AppStatus.RUNNING if result.returncode == 0 else "error",
                "returncode": result.returncode,
                "stdout": result.stdout[:4000],
                "stderr": result.stderr[:4000],
                "command": cmd_str,
            }
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "command": cmd_str}
