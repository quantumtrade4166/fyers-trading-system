"""
JARVIS Constants
================
Centralized constants for the JARVIS system.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
JARVIS_DIR = PROJECT_ROOT / "jarvis"
DB_PATH = os.environ.get("JARVIS_DB_PATH", str(JARVIS_DIR / "data" / "jarvis.db"))
KILL_SWITCH_FILE = os.environ.get("JARVIS_KILL_SWITCH", str(JARVIS_DIR / "data" / "kill_switch.flag"))
TIMELINE_DIR = JARVIS_DIR / "timeline"

# ── Database ──────────────────────────────────────────────────────────────
DB_WAL_MODE = True
DB_BUSY_TIMEOUT_MS = 5000

# ── Permission Levels ─────────────────────────────────────────────────────
class PermissionLevel:
    L0_READ = 0       # Read-only, listing, status checks
    L1_LOW = 1        # Open files, switch windows, launch authorized apps
    L2_MEDIUM = 2     # Shell commands, restart services
    L3_HIGH = 3       # Delete files, modify config, expose credentials
    L4_FINANCIAL = 4  # Live trades, financial decisions


class PermissionDecision:
    AUTO_APPROVED = "auto_approved"
    CONFIRMED = "confirmed"
    DENIED = "denied"
    OVERRIDDEN = "overridden"


# ── Goal/Task Status ──────────────────────────────────────────────────────
class GoalStatus:
    PLANNING = "planning"
    ACTIVE = "active"
    AT_RISK = "at_risk"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class TaskState:
    PENDING = "pending"
    ACTIVE = "active"
    BLOCKED = "blocked"
    WAITING = "waiting"
    FAILED = "failed"
    COMPLETED = "completed"
    VERIFIED = "verified"
    OVERDUE = "overdue"
    STALE = "stale"


# ── Desktop Actions ───────────────────────────────────────────────────────
class DesktopAction:
    LAUNCH = "launch"
    CLOSE = "close"
    FOCUS = "focus"
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"
    SWITCH = "switch"
    OPEN_FILE = "open_file"
    OPEN_FOLDER = "open_folder"
    OPEN_URL = "open_url"
    EXECUTE = "execute"
    RESTART = "restart"
    STOP = "stop"
    STATUS = "status"
    CLICK = "click"
    TYPE = "type"
    KEYPRESS = "keypress"


class AppStatus:
    UNKNOWN = "unknown"
    RUNNING = "running"
    NOT_RUNNING = "not_running"
    ERROR = "error"


class HealthStatus:
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


# ── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_TZ = "Australia/Sydney"
DEFAULT_AUDIT_LIMIT = 50
DEFAULT_GOAL_PRIORITY = 5

# ── Memory Preferences ────────────────────────────────────────────────────
class MemoryPref:
    """Predefined memory preference keys."""

    USER_NAME = "user:name"
    USER_PREFERRED_TZ = "user:preferred_timezone"
    TRADING_STYLE = "trading:style"
    DEFAULT_PROJECT = "project:default"
    CAPITAL_SIZE = "trading:capital_size"
    LIVE_TRADING = "trading:live"
    ALWAYS_CONFIRM_LIVE = "trading:always_confirm_live"
