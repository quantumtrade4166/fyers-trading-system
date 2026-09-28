"""
JARVIS — Agent Loop
====================
Proactive background monitoring loop using APScheduler.

Runs periodic checks:
  - VPS health (ping + SSH-ready check)
  - Dashboard responsiveness
  - Data freshness (last candle timestamp)
  - Disk space

Architecture:
  - JARVIS initializes the loop at startup
  - Each check runs independently
  - Results go to the timeline + audit trail
  - Configurable intervals per check
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore

from .logging_config import get_logger
from .memory.timeline import Timeline, new_event_id, utc_now
from .security.audit import AuditTrail
from .security.kill_switch import get_kill_switch

log = get_logger("agent_loop")

# Default check intervals (seconds)
DEFAULT_INTERVALS = {
    "vps_ping": 300,        # 5 minutes
    "data_freshness": 600,  # 10 minutes
    "disk_space": 1800,     # 30 minutes
    "dashboard": 300,       # 5 minutes
}


class CheckResult:
    """Result of a single health check."""

    def __init__(self, name: str, status: str, message: str = "", data: dict | None = None) -> None:
        self.name = name
        self.status = status  # "healthy", "degraded", "failed", "skipped"
        self.message = message
        self.data = data or {}
        self.timestamp = utc_now()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "data": self.data,
            "timestamp": self.timestamp,
        }


class AgentLoop:
    """
    Background monitoring loop for JARVIS.

    Starts with JARVIS initialization. Runs independent checks at configured
    intervals. Stops cleanly when the kill switch is activated.
    """

    def __init__(
        self,
        timeline: Optional[Timeline] = None,
        audit: Optional[AuditTrail] = None,
        intervals: dict[str, int] | None = None,
        enabled: bool = True,
    ) -> None:
        self._timeline = timeline
        self._audit = audit
        self._intervals = intervals or DEFAULT_INTERVALS.copy()
        self._enabled = enabled
        self._scheduler: Optional[Any] = None
        self._checks: dict[str, Callable[[], CheckResult]] = {}
        self._last_results: dict[str, CheckResult] = {}
        self._running = False

    def register_check(self, name: str, func: Callable[[], CheckResult]) -> None:
        """Register a custom health check."""
        self._checks[name] = func

    def register_default_checks(self) -> None:
        """Register the standard set of health checks."""
        self._checks["vps_ping"] = self._check_vps
        self._checks["data_freshness"] = self._check_data_freshness
        self._checks["disk_space"] = self._check_disk_space
        self._checks["dashboard"] = self._check_dashboard

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background monitoring loop."""
        if not self._enabled or self._running:
            return

        self.register_default_checks()

        self._scheduler = AsyncIOScheduler(
            timezone="UTC",
            misfire_grace_time=60,
        )

        for name, check_fn in self._checks.items():
            interval = self._intervals.get(name, 300)
            self._scheduler.add_job(
                self._run_check,
                trigger=IntervalTrigger(seconds=interval),
                args=[name, check_fn],
                id=f"check_{name}",
                name=f"JARVIS check: {name}",
                max_instances=1,
                coalesce=True,
            )

        self._scheduler.start()
        self._running = True
        log.info("agent_loop_started", checks=list(self._checks.keys()), intervals=self._intervals)

        # Record start event
        await self._record_event("agent_loop_started", "Agent loop started", {})

    async def stop(self) -> None:
        """Stop the background monitoring loop."""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        self._running = False
        log.info("agent_loop_stopped")
        await self._record_event("agent_loop_stopped", "Agent loop stopped", {})

    @property
    def is_running(self) -> bool:
        return self._running

    def get_last_results(self) -> dict[str, dict]:
        """Return the last result from each check as dicts."""
        return {k: v.to_dict() for k, v in self._last_results.items()}

    # ── Check Execution ──────────────────────────────────────────────────────

    async def _run_check(self, name: str, check_fn: Callable[[], CheckResult]) -> None:
        """Run a single check and record the result."""
        if get_kill_switch().is_active:
            log.debug("check_skipped_kill_switch", name=name)
            return

        try:
            result = check_fn()
            self._last_results[name] = result

            if result.status in ("failed", "degraded"):
                log.warning("check_degraded", name=name, status=result.status, message=result.message)
            else:
                log.debug("check_ok", name=name, status=result.status)

            await self._record_event(
                f"check_{name}", f"Agent check: {name} = {result.status}",
                result.to_dict(),
            )

            # Audit
            if self._audit:
                try:
                    await self._audit.record(
                        actor="agent_loop",
                        action=f"check_{name}",
                        result=result.status,
                        details=result.message,
                    )
                except Exception:
                    pass

        except Exception as e:
            log.error("check_error", name=name, error=str(e))

    # ── Health Checks ────────────────────────────────────────────────────────

    def _check_vps(self) -> CheckResult:
        """Ping the VPS to check connectivity."""
        vps_ip = "144.79.166.103"
        port = 22  # SSH port

        from .vps_controller import running_on_vps
        if running_on_vps():
            # We ARE the VPS: never open sockets to our own sshd every 5 min.
            return CheckResult(
                name="vps_ping",
                status="healthy",
                message="Running on the VPS itself",
                data={"vps_ip": vps_ip, "mode": "local"},
            )

        try:
            sock = socket.create_connection((vps_ip, port), timeout=5)
            sock.close()
            return CheckResult(
                name="vps_ping",
                status="healthy",
                message=f"VPS {vps_ip} reachable on port {port}",
                data={"vps_ip": vps_ip, "port": port},
            )
        except (OSError, socket.timeout) as e:
            return CheckResult(
                name="vps_ping",
                status="failed",
                message=f"VPS {vps_ip} unreachable: {e}",
                data={"vps_ip": vps_ip, "error": str(e)},
            )

    def _check_data_freshness(self) -> CheckResult:
        """Check how fresh the downloaded market data is."""
        try:
            from jarvis.integrations.fyers_loader import get_data_status
            # Run synchronously (the loader handles its own I/O)
            loop = asyncio.new_event_loop()
            try:
                status = loop.run_until_complete(get_data_status())
            finally:
                loop.close()

            if status.get("error"):
                return CheckResult(
                    name="data_freshness",
                    status="degraded",
                    message=status["error"],
                    data=status,
                )

            latest = status.get("latest_date", "unknown")
            return CheckResult(
                name="data_freshness",
                status="healthy",
                message=f"Latest data: {latest}",
                data=status,
            )
        except ImportError:
            return CheckResult(
                name="data_freshness",
                status="skipped",
                message="Fyers loader not available",
            )
        except Exception as e:
            return CheckResult(
                name="data_freshness",
                status="degraded",
                message=f"Data freshness check failed: {e}",
                data={"error": str(e)},
            )

    def _check_disk_space(self) -> CheckResult:
        """Check disk space on the data drive."""
        try:
            # Check G: drive (primary data drive)
            usage = shutil.disk_usage("G:\\")
            free_gb = usage.free / (1024**3)
            total_gb = usage.total / (1024**3)
            pct_used = (usage.used / usage.total) * 100

            if free_gb < 10:
                status = "failed"
                message = f"Low disk space: {free_gb:.1f} GB free ({pct_used:.0f}% used)"
            elif free_gb < 50:
                status = "degraded"
                message = f"Disk space moderate: {free_gb:.1f} GB free ({pct_used:.0f}% used)"
            else:
                status = "healthy"
                message = f"Disk OK: {free_gb:.1f} GB free ({pct_used:.0f}% used)"

            return CheckResult(
                name="disk_space",
                status=status,
                message=message,
                data={
                    "drive": "G:",
                    "free_gb": round(free_gb, 1),
                    "total_gb": round(total_gb, 1),
                    "pct_used": round(pct_used, 1),
                },
            )
        except Exception as e:
            return CheckResult(
                name="disk_space",
                status="failed",
                message=f"Could not check disk space: {e}",
                data={"error": str(e)},
            )

    def _check_dashboard(self) -> CheckResult:
        """Check dashboard responsiveness."""
        url = "https://dash.trading.contact"
        try:
            # Quick socket connect test (faster than HTTP)
            host = "dash.trading.contact"
            port = 443
            sock = socket.create_connection((host, port), timeout=10)
            sock.close()
            return CheckResult(
                name="dashboard",
                status="healthy",
                message=f"Dashboard {url} reachable",
                data={"url": url},
            )
        except (OSError, socket.timeout) as e:
            return CheckResult(
                name="dashboard",
                status="degraded",
                message=f"Dashboard {url} unreachable: {e}",
                data={"url": url, "error": str(e)},
            )

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _record_event(self, event_type: str, description: str, data: dict) -> None:
        """Record an event to the timeline."""
        if self._timeline:
            try:
                await self._timeline.record(
                    event_type=event_type,
                    description=description,
                    metadata=data,
                    actor="agent_loop",
                )
            except Exception:
                pass
