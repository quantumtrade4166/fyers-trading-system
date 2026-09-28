"""
JARVIS Config
=============
Centralized configuration for the JARVIS system.

Rules:
  - All sensitive values (API keys, tokens) are loaded from environment variables.
  - Default values are safe fallbacks.
  - Configuration is immutable after first access (frozen dataclass).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class JARVISConfig:
    """JARVIS system configuration."""

    # ── Database ─────────────────────────────────────────────────────────
    db_path: str = field(default_factory=lambda: os.environ.get(
        "JARVIS_DB_PATH",
        str(Path(__file__).resolve().parent.parent / "jarvis" / "data" / "jarvis.db"),
    ))
    db_wal_mode: bool = True
    db_busy_timeout_ms: int = 5000

    # ── Logging ──────────────────────────────────────────────────────────
    log_level: str = os.environ.get("JARVIS_LOG_LEVEL", "INFO")
    log_file_max_mb: int = 10
    log_file_backups: int = 5

    # ── Desktop Control ──────────────────────────────────────────────────
    desktop_enabled: bool = os.environ.get("JARVIS_DESKTOP", "true").lower() == "true"
    gui_automation_enabled: bool = os.environ.get("JARVIS_GUI", "false").lower() == "true"

    # ── Kill Switch ──────────────────────────────────────────────────────
    kill_switch_path: str = os.environ.get(
        "JARVIS_KILL_SWITCH",
        str(Path(__file__).resolve().parent.parent / "jarvis" / "data" / "kill_switch.flag"),
    )

    # ── Timeline ─────────────────────────────────────────────────────────
    timeline_dir: str = os.environ.get(
        "JARVIS_TIMELINE",
        str(Path(__file__).resolve().parent.parent / "jarvis" / "timeline"),
    )

    # ── App Registry ─────────────────────────────────────────────────────
    app_registry_path: str = ""

    # ── Learning ─────────────────────────────────────────────────────────
    learning_enabled: bool = True
    feedback_storage_path: str = ""

    # ── Email Processing ─────────────────────────────────────────────────
    email_enabled: bool = False
    email_auto_processing: bool = False  # L2 — requires confirmation

    # ── Financial Limits ─────────────────────────────────────────────────
    financial_action_requires_confirmation: bool = True

    # ── Derive paths ─────────────────────────────────────────────────────
    def __post_init__(self) -> None:
        # Resolve relative paths from the jarvis package
        jarvis_root = Path(__file__).resolve().parent.parent
        data_dir = jarvis_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)


# ── Singleton Config ──────────────────────────────────────────────────────
_config: JARVISConfig | None = None


def get_config() -> JARVISConfig:
    global _config
    if _config is None:
        _config = JARVISConfig()
    return _config


def reload_config() -> JARVISConfig:
    global _config
    _config = JARVISConfig()
    return _config
