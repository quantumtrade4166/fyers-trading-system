"""
JARVIS — VPS Controller
========================
SSH-based control of the VPS (144.79.166.103).

Capabilities:
  - Check VPS health (ping, disk, memory, running processes)
  - Restart trading engines (DualMom, dashboard, etc.)
  - Execute arbitrary commands
  - Read VPS log files

Requires: SSH key-based auth (no password). The VPS must have the
public key in ~/.ssh/authorized_keys.

Usage:
    from jarvis.vps_controller import VPSController
    ctrl = VPSController()
    result = await ctrl.execute("restart dashboard")
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from jarvis.logging_config import get_logger
from jarvis.constants import PermissionLevel

log = get_logger("vps_controller")

# ── VPS Configuration ──────────────────────────────────────────────────────

VPS_IP = "144.79.166.103"
VPS_SSH_PORT = 22
VPS_USER = "ubuntu"

# Known engine service names on the VPS
VPS_ENGINES = {
    "dashboard": "uvicorn dashboard.main:app --host 0.0.0.0 --port 8000",
    "dualmom_kite": "python main.py (DualMom Kite)",
    "dualmom_kotak": "python main.py (DualMom Kotak)",
    "nifty_pivot": "python main.py (Nifty Pivot)",
    "vwap_strangle": "python main.py (Vwap Strangle)",
    "btc_vwap": "python main.py (BTC Vwap)",
}

# Commands that can be run remotely
_SAFE_COMMANDS: dict[str, str] = {
    "restart dashboard": "sudo systemctl restart dashboard || kill -HUP $(pgrep -f 'uvicorn dashboard')",
    "restart dualmom_kite": "sudo systemctl restart dualmom_kite || kill -HUP $(pgrep -f 'dualmom_kite')",
    "restart dualmom_kotak": "sudo systemctl restart dualmom_kotak",
    "restart nifty_pivot": "sudo systemctl restart nifty_pivot",
    "restart vwap_strangle": "sudo systemctl restart vwap_strangle",
    "restart btc_vwap": "sudo systemctl restart btc_vwap",
    "restart all": "for svc in dashboard dualmom_kite dualmom_kotak nifty_pivot vwap_strangle btc_vwap; do sudo systemctl restart $svc 2>/dev/null; done",
    "check dashboard": "curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/health 2>/dev/null || echo 'unreachable'",
    "check dualmom_kite": "pgrep -f 'dualmom_kite' && echo 'running' || echo 'stopped'",
    "check dualmom_kotak": "pgrep -f 'dualmom_kotak' && echo 'running' || echo 'stopped'",
    "check nifty_pivot": "pgrep -f 'nifty_pivot' && echo 'running' || echo 'stopped'",
    "check vwap_strangle": "pgrep -f 'vwap_strangle' && echo 'running' || echo 'stopped'",
    "check btc_vwap": "pgrep -f 'btc_vwap' && echo 'running' || echo 'stopped'",
    "check all": "for svc in dashboard dualmom_kite dualmom_kotak nifty_pivot vwap_strangle btc_vwap; do echo \"$svc: $(pgrep -f $svc > /dev/null && echo 'running' || echo 'stopped')\"; done",
    "disk": "df -h / | tail -1",
    "memory": "free -h | head -2",
    "cpu": "top -bn1 | head -5",
    "logs dashboard": "journalctl -u dashboard --no-pager -n 50 2>/dev/null || tail -50 /var/log/dashboard.log 2>/dev/null || echo 'no logs'",
    "logs dualmom_kite": "journalctl -u dualmom_kite --no-pager -n 50 2>/dev/null || tail -50 ~/dual*/log*.log 2>/dev/null || echo 'no logs'",
    "uptime": "uptime && cat /proc/loadavg",
    "who": "who",
    "ps": "ps aux | grep -E 'python|uvicorn|node' | grep -v grep",
}


class VPSConnectionError(Exception):
    """Raised when VPS is unreachable."""
    pass


class VPSController:
    """
    SSH-based VPS controller.

    Requires: asyncssh or paramiko installed. Falls back to subprocess
    ssh if neither is available (slower but works).
    """

    def __init__(
        self,
        host: str = VPS_IP,
        port: int = VPS_SSH_PORT,
        user: str = VPS_USER,
        use_key_auth: bool = True,
        key_path: str = "~/.ssh/id_rsa",
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.use_key_auth = use_key_auth
        self.key_path = key_path
        self._connected = False

    async def execute(self, command: str) -> dict:
        """
        Execute a command on the VPS.

        Args:
            command: A named command from _SAFE_COMMANDS, or a raw shell command.

        Returns:
            Result dict with success, stdout, stderr, and timing.
        """
        # Resolve named commands
        if command in _SAFE_COMMANDS:
            shell_cmd = _SAFE_COMMANDS[command]
        else:
            shell_cmd = command

        start = time.monotonic()
        try:
            output, stderr = await self._ssh_run(shell_cmd)
            elapsed = time.monotonic() - start
            return {
                "status": "ok",
                "command": command,
                "shell_cmd": shell_cmd,
                "stdout": output.strip(),
                "stderr": stderr.strip(),
                "elapsed_s": round(elapsed, 2),
                "vps": self.host,
            }
        except VPSConnectionError as e:
            return {
                "status": "error",
                "command": command,
                "error": str(e),
                "vps": self.host,
            }
        except Exception as e:
            return {
                "status": "error",
                "command": command,
                "error": f"Unexpected error: {e}",
                "vps": self.host,
            }

    async def check_connectivity(self) -> dict:
        """Check if the VPS is reachable (socket test)."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=5,
            )
            writer.close()
            await writer.wait_closed()
            self._connected = True
            return {"status": "ok", "vps": self.host, "port": self.port, "reachable": True}
        except (OSError, asyncio.TimeoutError) as e:
            self._connected = False
            return {"status": "error", "vps": self.host, "reachable": False, "error": str(e)}

    async def health_check(self) -> dict:
        """Run a comprehensive health check on the VPS."""
        connectivity = await self.check_connectivity()
        if connectivity["status"] != "ok":
            return connectivity

        # Run lightweight checks in parallel
        tasks = [
            self.execute("uptime"),
            self.execute("disk"),
            self.execute("memory"),
            self.execute("check dashboard"),
            self.execute("check all"),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        return {
            "status": "ok",
            "vps": self.host,
            "connectivity": connectivity,
            "checks": {
                "uptime": results[0].get("stdout", "") if not isinstance(results[0], Exception) else str(results[0]),
                "disk": results[1].get("stdout", "") if not isinstance(results[1], Exception) else str(results[1]),
                "memory": results[2].get("stdout", "") if not isinstance(results[2], Exception) else str(results[2]),
                "dashboard": results[3].get("stdout", "") if not isinstance(results[3], Exception) else str(results[3]),
                "engines": results[4].get("stdout", "") if not isinstance(results[4], Exception) else str(results[4]),
            },
        }

    async def restart_engine(self, engine_name: str) -> dict:
        """Restart a specific trading engine."""
        cmd = f"restart {engine_name}"
        return await self.execute(cmd)

    # ── SSH Implementation ────────────────────────────────────────────────

    async def _ssh_run(self, shell_cmd: str) -> tuple[str, str]:
        """Run a command over SSH, returning (stdout, stderr)."""
        # Try asyncssh first (fast, native async)
        try:
            return await self._ssh_run_asyncssh(shell_cmd)
        except ImportError:
            pass

        # Fallback to paramiko
        try:
            return await self._ssh_run_paramiko(shell_cmd)
        except ImportError:
            pass

        # Last resort: subprocess ssh
        return await self._ssh_run_subprocess(shell_cmd)

    async def _ssh_run_asyncssh(self, shell_cmd: str) -> tuple[str, str]:
        """Run via asyncssh."""
        import asyncssh  # type: ignore

        key_file = None
        if self.use_key_auth:
            import os
            key_file = os.path.expanduser(self.key_path)

        async with asyncssh.connect(
            self.host,
            port=self.port,
            username=self.user,
            client_keys=[key_file] if key_file else None,
            known_hosts=None,  # Accept any host key (not ideal for prod)
            connect_timeout=10,
        ) as conn:
            result = await conn.run(shell_cmd, check=False)
            return result.stdout or "", result.stderr or ""

    async def _ssh_run_paramiko(self, shell_cmd: str) -> tuple[str, str]:
        """Run via paramiko (wrapped in asyncio)."""
        import paramiko  # type: ignore
        import os

        key_file = os.path.expanduser(self.key_path) if self.use_key_auth else None

        loop = asyncio.get_event_loop()
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        def _connect():
            ssh.connect(
                self.host,
                port=self.port,
                username=self.user,
                key_filename=key_file,
                timeout=10,
            )

        await loop.run_in_executor(None, _connect)
        try:
            stdin, stdout, stderr = ssh.exec_command(shell_cmd, timeout=30)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            return out, err
        finally:
            ssh.close()

    async def _ssh_run_subprocess(self, shell_cmd: str) -> tuple[str, str]:
        """Run via system ssh command (fallback)."""
        import os
        key_file = os.path.expanduser(self.key_path) if self.use_key_auth else ""
        ssh_args = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
        if key_file:
            ssh_args.extend(["-i", key_file])
        ssh_args.append(f"{self.user}@{self.host}")
        ssh_args.append(shell_cmd)

        proc = await asyncio.create_subprocess_exec(
            *ssh_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode is not None and proc.returncode != 0:
            # stderr might contain connection errors
            if "Connection refused" in stderr.decode() or "No route" in stderr.decode():
                raise VPSConnectionError(f"SSH to {self.host}:{self.port} failed: {stderr.decode().strip()}")
        return stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")

    # ── Parsing Helpers ────────────────────────────────────────────────────

    @staticmethod
    def parse_health(output: str) -> dict:
        """Parse a health check output into structured data."""
        result = {"raw": output, "engines": {}}
        for line in output.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Memory line (must check before generic ":" split)
            if line.startswith("Mem:"):
                result["memory"] = line
            elif ":" in line and not line.startswith("(") and not line.startswith("/"):
                # Engine status line like "dualmom_kite: running"
                parts = line.split(":", 1)
                if len(parts) == 2:
                    svc, state = parts[0].strip(), parts[1].strip()
                    if svc in VPS_ENGINES:
                        result["engines"][svc] = state
            elif line.startswith("/") and "G" in line:
                # Disk line like "/dev/sda1  50G  30G  20G  60% /"
                result["disk"] = line
            elif re.match(r"\d+\.\d+", line) and "up" not in line:
                result["uptime"] = line
        return result


# ── Safe command list (for display) ────────────────────────────────────────

SAFE_COMMANDS = list(_SAFE_COMMANDS.keys())
