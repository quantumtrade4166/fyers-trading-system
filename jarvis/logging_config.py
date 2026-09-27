"""
JARVIS Logging Configuration
=============================
Centralized logging with trace_id propagation.

Rules:
  - JARVIS operations use the 'jarvis' logger.
  - Database operations use 'database'.
  - Desktop control uses 'desktop_control'.
  - Each log entry includes a trace_id when available.
  - ERROR level logs are always persisted.
  - Module-level logging_config should never be imported into tests.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Encoding Fix (Claude Code rule) ────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ── Trace ID Context ──────────────────────────────────────────────────────
_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_trace_id(trace_id: str) -> None:
    _trace_id_var.set(trace_id)


def get_trace_id() -> str | None:
    return _trace_id_var.get()


# ── Thread-local storage for trace IDs ────────────────────────────────────
_thread_local = threading.local()


# ── Custom Formatter ──────────────────────────────────────────────────────
class TraceFormatter(logging.Formatter):
    """Include trace_id in log records."""

    def format(self, record: logging.LogRecord) -> str:
        trace_id = get_trace_id()
        if trace_id:
            record.msg = f"[{trace_id}] {record.msg}"
        return super().format(record)


# ── Logger Factory ────────────────────────────────────────────────────────
_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "jarvis" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / "jarvis.log"


def _setup_root() -> None:
    """Configure the root logger once."""
    root = logging.getLogger()
    if root.handlers:
        return  # Already configured

    root.setLevel(logging.WARNING)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(console)

    # File handler (rotating, 10MB max)
    try:
        file_handler = RotatingFileHandler(
            _LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root.addHandler(file_handler)
    except OSError as e:
        root.warning(f"Could not set up file logging: {e}")


class StructuredLogger:
    """Thin wrapper around logging.Logger that supports structured keyword logging.

    Usage:
        log = get_logger("module")
        log.info("event_name", key=value)  # → "event_name key=value"
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _format(self, msg: str, kwargs: dict) -> str:
        if kwargs:
            extras = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
            return f"{msg} {extras}"
        return msg

    def debug(self, msg: str, **kwargs) -> None:
        self._logger.debug(self._format(msg, kwargs))

    def info(self, msg: str, **kwargs) -> None:
        self._logger.info(self._format(msg, kwargs))

    def warning(self, msg: str, **kwargs) -> None:
        self._logger.warning(self._format(msg, kwargs))

    def error(self, msg: str, **kwargs) -> None:
        self._logger.error(self._format(msg, kwargs))

    def critical(self, msg: str, **kwargs) -> None:
        self._logger.critical(self._format(msg, kwargs))


def get_logger(name: str) -> StructuredLogger:
    """
    Get a named structured logger for the JARVIS system.

    Usage:
        from jarvis.logging_config import get_logger
        log = get_logger("database")
        log.info("event_name", key=value)  # Structured logging
    """
    _setup_root()
    logger = logging.getLogger(f"jarvis.{name}")
    logger.setLevel(logging.DEBUG)
    return StructuredLogger(logger)
