"""
JARVIS Agent — Monitoring
==========================
VPS and system monitoring agent.

Responsibilities:
  - Monitor VPS health (CPU, memory, disk)
  - Check dashboard status
  - Monitor scheduled tasks
  - Alert on anomalies
"""

from __future__ import annotations

from typing import Any, Optional

from ..logging_config import get_logger
from .base_agent import BaseAgent

log = get_logger("agent_monitoring")


class MonitoringAgent(BaseAgent):
    """System and VPS monitoring agent."""

    name = "monitoring_agent"
    description = "Monitors VPS, dashboard, and scheduled task health"
    permission_level = 1

    async def _execute(self, command: str, **kwargs: Any) -> dict:
        cmd = command.lower()

        if "vps" in cmd:
            return await self._check_vps()
        elif "dashboard" in cmd:
            return await self._check_dashboard()
        elif "tasks" in cmd:
            return await self._check_scheduled_tasks()
        else:
            return {
                "status": "ok",
                "agent": self.name,
                "message": "Monitoring agent ready. Use: vps, dashboard, tasks",
            }

    async def _check_vps(self) -> dict:
        """Check VPS health."""
        return {
            "status": "ok",
            "agent": self.name,
            "message": "VPS check requires SSH — Phase 5",
            "vps_ip": "144.79.166.103",
        }

    async def _check_dashboard(self) -> dict:
        """Check dashboard health."""
        return {
            "status": "ok",
            "agent": self.name,
            "message": "Dashboard check requires HTTP request — Phase 2",
            "dashboard_url": "https://dash.trading.contact",
        }

    async def _check_scheduled_tasks(self) -> dict:
        """Check scheduled tasks on the local machine."""
        try:
            import win32com.client  # type: ignore
            scheduler = win32com.client.Dispatch("Schedule.Service")
            scheduler.Connect()
            folder = scheduler.GetFolder("\\")
            tasks = folder.GetTasks(0)  # 0 = all tasks

            task_list = []
            for i in range(1, tasks.Count + 1):
                task = tasks.Item(i)
                task_list.append({
                    "name": task.Name,
                    "state": str(task.State),
                    "last_run": str(task.LastRunTime),
                    "next_run": str(task.NextRunTime),
                })

            return {
                "status": "ok",
                "agent": self.name,
                "tasks": task_list[:20],  # Limit output
            }
        except ImportError:
            return {
                "status": "partial",
                "agent": self.name,
                "message": "pywin32 not installed — use Task Scheduler UI",
            }
        except Exception as e:
            return {
                "status": "error",
                "agent": self.name,
                "message": f"Failed to query tasks: {e}",
            }
