"""
JARVIS Exceptions
=================
Custom exception hierarchy for the JARVIS system.

Rules:
  - Every exception has a code string for programmatic handling.
  - HTTP error codes are included for API responses.
  - User-facing messages are plain English.
"""

from __future__ import annotations


class JarvisError(Exception):
    """Base exception for all JARVIS errors."""

    def __init__(self, message: str, *, code: str = "UNKNOWN", http_code: int = 500):
        self.message = message
        self.code = code
        self.http_code = http_code
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {
            "error": self.message,
            "code": self.code,
            "http_code": self.http_code,
        }


# ── Database Errors ───────────────────────────────────────────────────────
class DatabaseError(JarvisError):
    """Database-related errors."""

    def __init__(self, message: str, *, code: str = "DB_ERROR", http_code: int = 500):
        super().__init__(message, code=code, http_code=http_code)


class SchemaError(DatabaseError):
    """Schema creation or migration errors."""

    def __init__(self, message: str, *, code: str = "SCHEMA_ERROR"):
        super().__init__(message, code=code, http_code=500)


# ── Memory Errors ────────────────────────────────────────────────────────
class MemoryError(JarvisError):
    """Memory system errors."""

    def __init__(self, message: str, *, code: str = "MEMORY_ERROR", http_code: int = 400):
        super().__init__(message, code=code, http_code=http_code)


class ImmutableLedgerError(MemoryError):
    """Attempt to modify an immutable ledger."""

    def __init__(self, message: str, *, code: str = "IMMUTABLE"):
        super().__init__(message, code=code, http_code=403)


class GoalNotFoundError(MemoryError):
    """Goal not found."""

    def __init__(self, message: str, *, code: str = "GOAL_NOT_FOUND"):
        super().__init__(message, code=code, http_code=404)


class TaskNotFoundError(MemoryError):
    """Task not found."""

    def __init__(self, message: str, *, code: str = "TASK_NOT_FOUND"):
        super().__init__(message, code=code, http_code=404)


# ── Security Errors ──────────────────────────────────────────────────────
class SecurityError(JarvisError):
    """Security-related errors."""

    def __init__(self, message: str, *, code: str = "SECURITY_ERROR", http_code: int = 403):
        super().__init__(message, code=code, http_code=http_code)


class PermissionDeniedError(SecurityError):
    """Permission check failed."""

    def __init__(self, message: str, *, code: str = "PERMISSION_DENIED"):
        super().__init__(message, code=code, http_code=403)


class KillSwitchActiveError(SecurityError):
    """Kill switch is active, blocking the action."""

    def __init__(self, message: str, *, code: str = "KILL_SWITCH_ACTIVE"):
        super().__init__(message, code=code, http_code=503)


# ── Desktop Control Errors ───────────────────────────────────────────────
class DesktopError(JarvisError):
    """Desktop control errors."""

    def __init__(self, message: str, *, code: str = "DESKTOP_ERROR", http_code: int = 500):
        super().__init__(message, code=code, http_code=http_code)


class AppNotFoundError(DesktopError):
    """Application not found or not executable."""

    def __init__(self, message: str, *, code: str = "APP_NOT_FOUND"):
        super().__init__(message, code=code, http_code=404)


# ── Agent Errors ─────────────────────────────────────────────────────────
class AgentError(JarvisError):
    """Agent execution errors."""

    def __init__(self, message: str, *, code: str = "AGENT_ERROR", http_code: int = 500):
        super().__init__(message, code=code, http_code=http_code)


class TimeoutError(JarvisError):
    """Operation timed out."""

    def __init__(self, message: str, *, code: str = "TIMEOUT", http_code: int = 408):
        super().__init__(message, code=code, http_code=http_code)


# ── Configuration Errors ─────────────────────────────────────────────────
class ConfigError(JarvisError):
    """Configuration errors."""

    def __init__(self, message: str, *, code: str = "CONFIG_ERROR", http_code: int = 500):
        super().__init__(message, code=code, http_code=http_code)


# ── Integration Errors ───────────────────────────────────────────────────
class IntegrationError(JarvisError):
    """External integration errors (Fyers, dashboard, etc.)."""

    def __init__(self, message: str, *, code: str = "INTEGRATION_ERROR", http_code: int = 502):
        super().__init__(message, code=code, http_code=http_code)
