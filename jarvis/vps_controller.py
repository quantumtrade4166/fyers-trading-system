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
import base64
import json
import os
import re
import socket
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
VPS_USER = "Administrator"
# Key used only when JARVIS runs OFF the VPS (e.g. on the local PC)
VPS_KEY_PATH = os.path.expanduser("~/.ssh/id_rsa")
VPS_HOSTNAME = "WIN-IK7N6SD2UBU"


def running_on_vps() -> bool:
    """True when this process is on the VPS itself.

    On the VPS, JARVIS must run commands LOCALLY. SSH-ing into itself
    (5 logins per health check) tripped the sshd throttle and locked
    everyone out — see memory note feedback_vps_ssh_hammering.
    """
    if os.environ.get("JARVIS_ON_VPS") == "1":
        return True
    try:
        if socket.gethostname().upper() == VPS_HOSTNAME:
            return True
        return VPS_IP in socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return False

# Known engine service names on the VPS (Windows scheduled tasks / processes)
VPS_ENGINES = {
    "dashboard": "uvicorn deployment.main:app --port 8000",
    "dualmom_kite": "dualmom_kite engine",
    "dualmom_kotak": "dualmom_kotak engine",
    "nifty_pivot": "nifty_pivot engine",
    "vwap_strangle": "vwap_strangle engine",
    "btc_vwap": "btc_vwap engine",
    "jarvis_api": "uvicorn jarvis.api.server:app --port 8081",
}

# Commands that can be run remotely — Windows PowerShell equivalents
_SAFE_COMMANDS: dict[str, str] = {
    "restart dashboard": "schtasks /Run /TN Dashboard 2>&1; Stop-Process -Name python -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -match 'deployment.main'} | Stop-Process -Force -ErrorAction SilentlyContinue; cd C:\\Users\\Administrator\\Desktop\\fyers_data_pipeline_git; Start-Process .venv\\Scripts\\python.exe -ArgumentList '-m','uvicorn','deployment.main:app','--host','0.0.0.0','--port','8000' -WindowStyle Hidden",
    "restart dualmom_kite": "schtasks /Run /TN DualMomKite 2>&1",
    "restart dualmom_kotak": "schtasks /Run /TN DualMomKotak 2>&1",
    "restart nifty_pivot": "schtasks /Run /TN NiftyPivotEngine 2>&1",
    "restart vwap_strangle": "schtasks /Run /TN VwapStrangleEngine 2>&1",
    "restart btc_vwap": "schtasks /Run /TN BTCVwapEngine 2>&1",
    "restart jarvis_api": "schtasks /Run /TN JarvisAPI 2>&1",
    "restart all": "schtasks /Run /TN Dashboard 2>&1; schtasks /Run /TN DualMomKite 2>&1; schtasks /Run /TN DualMomKotak 2>&1; schtasks /Run /TN NiftyPivotEngine 2>&1",
    "check dashboard": "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/' -UseBasicParsing -TimeoutSec 5; Write-Output ('HTTP ' + $r.StatusCode) } catch { Write-Output 'unreachable' }",
    "check jarvis_api": "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8081/health' -UseBasicParsing -TimeoutSec 5; Write-Output ('HTTP ' + $r.StatusCode) } catch { Write-Output 'unreachable' }",
    "check dualmom_kite": "Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -match 'dualmom_kite'} | Select-Object Id,ProcessName",
    "check dualmom_kotak": "Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -match 'dualmom_kotak'} | Select-Object Id,ProcessName",
    "check nifty_pivot": "Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -match 'nifty_pivot'} | Select-Object Id,ProcessName",
    "check vwap_strangle": "Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -match 'vwap_strangle'} | Select-Object Id,ProcessName",
    "check btc_vwap": "Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -match 'btc_vwap'} | Select-Object Id,ProcessName",
    "check all": "Get-Process -Name python -ErrorAction SilentlyContinue | Select-Object Id,@{N='CmdLine';E={(Get-WmiObject Win32_Process -Filter \"ProcessId = $($_.Id)\").CommandLine}} | Format-List",
    "disk": "Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='C:'\" | ForEach-Object { Write-Output (\"Total: \" + [math]::Round($_.Size/1GB,1) + \"GB, Free: \" + [math]::Round($_.FreeSpace/1GB,1) + \"GB\") }",
    "memory": "$os = Get-CimInstance Win32_OperatingSystem; $total = [math]::Round($os.TotalVisibleMemorySize/1MB, 1); $free = [math]::Round($os.FreePhysicalMemory/1MB, 1); $used = $total - $free; Write-Output \"Total: ${total}GB, Used: ${used}GB, Free: ${free}GB ($([math]::Round($free/$total*100,1))% free)\"",
    "cpu": "Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,LoadPercentage",
    "uptime": "$os = Get-CimInstance Win32_OperatingSystem; $span = (Get-Date) - $os.LastBootUpTime; $days = $span.Days; $hrs = $span.Hours; $mins = $span.Minutes; Write-Output \"Uptime: ${days}d ${hrs}h ${mins}m\"; Write-Output ('Boot: ' + $os.LastBootUpTime)",
    "logs dashboard": "Get-Content C:\\Users\\Administrator\\Desktop\\fyers_data_pipeline_git\\logs\\server.log -Tail 50 -ErrorAction SilentlyContinue || echo 'no logs'",
    "who": "whoami",
    "ps": "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe' OR Name = 'uvicorn.exe' OR Name = 'node.exe'\" | Select-Object ProcessId,Name,CommandLine | Format-List",
    "tasks": "schtasks /Query /FO LIST /V 2>&1 | Select-String -Pattern 'TaskName|Status|Last Run' -Context 0,0",
}


# Everything the health check needs, in ONE PowerShell run, emitted as JSON.
_HEALTH_SCRIPT = r"""
$os = Get-CimInstance Win32_OperatingSystem
$span = (Get-Date) - $os.LastBootUpTime
$d = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
try { $dash = 'HTTP ' + (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/' -UseBasicParsing -TimeoutSec 5).StatusCode } catch { $dash = 'unreachable' }
$eng = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine } | ForEach-Object {
    $c = ($_.CommandLine -replace '\s+', ' ').Trim()
    [pscustomobject]@{ pid = $_.ProcessId; cmd = $c.Substring(0, [math]::Min(200, $c.Length)) }
})
[pscustomobject]@{
    uptime    = ('{0}d {1}h {2}m (boot {3})' -f $span.Days, $span.Hours, $span.Minutes, $os.LastBootUpTime.ToString('yyyy-MM-dd HH:mm'))
    disk      = ('C: {0} GB free of {1} GB' -f [math]::Round($d.FreeSpace / 1GB, 1), [math]::Round($d.Size / 1GB, 1))
    memory    = ('{0} GB free of {1} GB' -f [math]::Round($os.FreePhysicalMemory / 1MB, 1), [math]::Round($os.TotalVisibleMemorySize / 1MB, 1))
    dashboard = $dash
    engines   = $eng
} | ConvertTo-Json -Depth 4 -Compress
"""

# Remote (off-VPS) SSH calls go through this lock: one login at a time, ever.
_REMOTE_LOCK = asyncio.Lock()


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
        key_path: str = VPS_KEY_PATH,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.use_key_auth = use_key_auth
        self.key_path = key_path
        self._connected = False
        self.local = host == VPS_IP and running_on_vps()

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
        if self.local:
            # We ARE the VPS — never poke our own sshd.
            self._connected = True
            return {"status": "ok", "vps": self.host, "mode": "local", "reachable": True}
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

        # ONE script gathers everything → one process locally, one SSH
        # login remotely. (The old version fired 5 SSH logins in parallel.)
        result = await self.execute(_HEALTH_SCRIPT)
        if result["status"] != "ok":
            return {**result, "connectivity": connectivity}
        try:
            checks = json.loads(result["stdout"])
        except (json.JSONDecodeError, TypeError):
            return {
                "status": "error",
                "vps": self.host,
                "connectivity": connectivity,
                "error": "health script returned non-JSON",
                "stdout": result.get("stdout", "")[:500],
                "stderr": result.get("stderr", "")[:500],
            }
        engines = checks.get("engines") or []
        if isinstance(engines, dict):  # single process → PowerShell emits an object
            engines = [engines]
        checks["engines"] = engines
        checks["engine_count"] = len(engines)
        return {
            "status": "ok",
            "vps": self.host,
            "connectivity": connectivity,
            "checks": checks,
        }

    async def restart_engine(self, engine_name: str) -> dict:
        """Restart a specific trading engine."""
        cmd = f"restart {engine_name}"
        return await self.execute(cmd)

    # ── SSH Implementation ────────────────────────────────────────────────

    async def _ssh_run(self, shell_cmd: str) -> tuple[str, str]:
        """Run a PowerShell command on the VPS, returning (stdout, stderr).

        On the VPS itself: runs locally, no SSH at all.
        Off the VPS: ONE SSH login at a time (serialised by a lock) so we
        can never burst the VPS sshd throttle.
        """
        full_cmd = f"$ProgressPreference = 'SilentlyContinue'; {shell_cmd}"
        encoded = base64.b64encode(full_cmd.encode("utf-16-le")).decode()

        if self.local:
            return await self._local_run(encoded)

        ps_cmd = f"powershell -NoProfile -EncodedCommand {encoded}"
        async with _REMOTE_LOCK:
            return await self._remote_run(ps_cmd)

    async def _local_run(self, encoded: str) -> tuple[str, str]:
        """Run an encoded PowerShell command on this machine."""
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        return (stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"))

    async def _remote_run(self, ps_cmd: str) -> tuple[str, str]:
        """Run over SSH — asyncssh, then paramiko, then the ssh binary."""
        # Try asyncssh first (fast, native async)
        try:
            return await self._ssh_run_asyncssh(ps_cmd)
        except ImportError:
            pass

        # Fallback to paramiko
        try:
            return await self._ssh_run_paramiko(ps_cmd)
        except ImportError:
            pass

        # Last resort: subprocess ssh
        return await self._ssh_run_subprocess(ps_cmd)

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

    async def _ssh_run_subprocess(self, ps_cmd: str) -> tuple[str, str]:
        """Run via system ssh command (fallback)."""
        import os
        key_file = os.path.expanduser(self.key_path) if self.use_key_auth else ""
        ssh_args = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
        if key_file:
            ssh_args.extend(["-i", key_file])
        ssh_args.append(f"{self.user}@{self.host}")
        ssh_args.append(ps_cmd)

        proc = await asyncio.create_subprocess_exec(
            *ssh_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode is not None and proc.returncode != 0:
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
