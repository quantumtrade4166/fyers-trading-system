"""
JARVIS — Natural Language Parser
==================================
Structured command parsing beyond keyword routing.

Handles patterns like:
  "launch VS Code in G:\\fyers_data_pipeline"
  "check data freshness"
  "create a goal to fix the backtest engine by Friday"
  "what's the status of the VPS"
  "run backtest for RELIANCE with BB strategy"
  "stop everything" (kill switch)

Architecture:
  - Intent classification via keyword patterns + regex
  - Entity extraction (app names, file paths, strategies, dates)
  - Confidence scoring
  - Fallback to goal creation for unrecognized commands
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


# ── Data Types ───────────────────────────────────────────────────────────────

@dataclass
class ParsedCommand:
    """Result of parsing a natural language command."""
    raw: str                          # Original command text
    intent: str                       # Primary intent (launch, status, create_goal, etc.)
    confidence: float                 # 0.0–1.0
    action: str = ""                  # Desktop action (launch, focus, close, etc.)
    target: str = ""                  # Target app/file/URL/etc.
    args: str = ""                    # Additional arguments
    project: str = ""                 # Associated project
    working_dir: str = ""             # Working directory hint
    entities: dict = field(default_factory=dict)  # Extracted entities
    fallback: bool = False            # True if treated as goal creation
    reason: str = ""                  # Why this intent was chosen


# ── Intent Patterns ─────────────────────────────────────────────────────────

_INTENT_PATTERNS: list[tuple[str, re.Pattern, float]] = [
    # Kill switch (highest priority, check first)
    ("kill_switch", re.compile(r"\b(stop\s+everything|emergency\s+stop|kill\s+switch|kill\s+all)\b", re.I), 0.95),

    # Desktop launch
    ("launch", re.compile(r"\b(launch|open|start|run)\s+(?:app\s+)?(.+?)(?:\s+in\s+(\S+))?$", re.I), 0.90),

    # Desktop focus
    ("focus", re.compile(r"\b(focus|bring\s+to\s+front|switch\s+to)\s+(.+)$", re.I), 0.85),

    # Desktop close
    ("close", re.compile(r"\b(close|quit|exit|kill)\s+(?:app\s+)?(.+)$", re.I), 0.85),

    # Desktop status
    ("status", re.compile(r"\b(what'?s\s+the\s+)?status\s+(?:of\s+)?(.+)?|(?:check|is)\s+(.+?)\s+(running|alive|ok|up)\b", re.I), 0.80),

    # Data freshness
    ("data_freshness", re.compile(r"\b(data\s+freshness|check\s+data|how\s+old\s+is\s+the\s+data|latest\s+candle)\b", re.I), 0.85),

    # Trigger data update
    ("data_update", re.compile(r"\b(update|refresh|download)\s+(?:the\s+)?data\b", re.I), 0.85),

    # Create goal
    ("create_goal", re.compile(r"\b(create|add|new|make)\s+(?:a\s+)?goal\s+(?:to\s+)?(.+)$", re.I), 0.80),

    # Create task
    ("create_task", re.compile(r"\b(create|add|new|make)\s+(?:a\s+)?task\s+(?:to\s+)?(.+)$", re.I), 0.80),

    # List goals
    ("list_goals", re.compile(r"\b(list|show|what)\s+(?:are\s+)?(?:my\s+)?goals?\b", re.I), 0.80),

    # List tasks
    ("list_tasks", re.compile(r"\b(list|show|what)\s+(?:are\s+)?(?:my\s+)?tasks?\b", re.I), 0.80),

    # Decisions
    ("decisions", re.compile(r"\b(decisions?|decided)\b", re.I), 0.60),

    # Agent: DualMom
    ("agent_dualmom", re.compile(r"\b(dualmom|dual\s+mom|momentum)\s+(.+)?\b", re.I), 0.70),

    # Agent: Fyers
    ("agent_fyers", re.compile(r"\b(fyers|data\s+pipeline|ingestion)\s+(.+)?\b", re.I), 0.70),

    # Agent: monitoring
    ("agent_monitoring", re.compile(r"\b(monitor|check\s+vps|check\s+dashboard|scheduled\s+tasks?)\b", re.I), 0.70),

    # Help
    ("help", re.compile(r"^(help|\?|commands|what\s+can\s+you\s+do)$", re.I), 0.95),
]


# ── Known App Names ──────────────────────────────────────────────────────────

_KNOWN_APPS = [
    "VS Code", "Visual Studio Code", "code",
    "Claude Code", "claude", "cc",
    "Obsidian",
    "Windows Terminal", "terminal", "wt",
    "PowerShell", "powershell", "ps",
    "Chrome", "google chrome", "browser",
    "File Explorer", "explorer", "files",
    "Git", "git bash",
    "Python",
    "JARVIS",
    "Task Scheduler", "schtasks",
    "Notepad", "notepad++",
    "PyCharm",
    "Firefox",
]


# ── Date/Deadline Extraction ─────────────────────────────────────────────────

_DEADLINE_PATTERNS = [
    (re.compile(r"\bby\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I), "next_weekday"),
    (re.compile(r"\bby\s+(tomorrow|today|next\s+week)\b", re.I), "relative"),
    (re.compile(r"\bby\s+(\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b", re.I), "date"),
    (re.compile(r"\b(\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b", re.I), "date_found"),
]


# ── Parser ───────────────────────────────────────────────────────────────────

class NLParser:
    """Natural language command parser for JARVIS."""

    def __init__(self) -> None:
        self._patterns = _INTENT_PATTERNS
        self._known_apps = _KNOWN_APPS

    def parse(self, command: str) -> ParsedCommand:
        """Parse a natural language command into structured intent + entities."""
        command = command.strip()
        if not command:
            return ParsedCommand(
                raw=command, intent="unknown", confidence=0.0,
                reason="empty command",
            )

        # Try pattern matching (highest confidence first)
        best: Optional[tuple[str, float, re.Match]] = None
        for intent, pattern, base_conf in self._patterns:
            m = pattern.search(command)
            if m:
                conf = base_conf
                # Boost confidence for longer matches
                if m.group(0):
                    conf = min(1.0, conf + 0.05 * len(m.group(0)) / len(command))
                if best is None or conf > best[1]:
                    best = (intent, conf, m)

        if best:
            intent, confidence, match = best
            parsed = ParsedCommand(
                raw=command,
                intent=intent,
                confidence=confidence,
                reason=f"matched pattern '{match.group(0)}'",
            )
            # Extract entities based on intent
            self._extract_entities(parsed, match, intent)
            return parsed

        # Fallback: treat as goal creation or generic action
        return self._fallback(command)

    def _extract_entities(self, parsed: ParsedCommand, match: re.Match, intent: str) -> None:
        """Extract structured entities from a matched pattern."""
        groups = match.groups()

        if intent == "launch":
            parsed.action = "launch"
            parsed.target = (groups[1] or "").strip() if len(groups) > 1 else ""
            parsed.working_dir = (groups[2] or "").strip() if len(groups) > 2 else ""
            # Normalize app name
            parsed.target = self._resolve_app_name(parsed.target)

        elif intent == "focus":
            parsed.action = "focus"
            parsed.target = (groups[1] or "").strip() if len(groups) > 1 else ""

        elif intent == "close":
            parsed.action = "close"
            parsed.target = (groups[1] or "").strip() if len(groups) > 1 else ""

        elif intent == "status":
            parsed.action = "status"
            if len(groups) >= 2 and groups[1]:
                parsed.target = groups[1].strip()
            elif len(groups) >= 3 and groups[2]:
                parsed.target = groups[2].strip()

        elif intent == "create_goal":
            parsed.intent = "create_goal"
            parsed.target = (groups[1] or "").strip() if len(groups) > 1 else ""
            parsed.entities["deadline"] = self._extract_deadline(parsed.raw)

        elif intent == "create_task":
            parsed.intent = "create_task"
            parsed.target = (groups[1] or "").strip() if len(groups) > 1 else ""
            parsed.entities["deadline"] = self._extract_deadline(parsed.raw)

        elif intent == "agent_dualmom":
            parsed.action = "agent"
            parsed.args = (groups[1] or "").strip() if len(groups) > 1 else ""

        elif intent == "agent_fyers":
            parsed.action = "agent"
            parsed.args = (groups[1] or "").strip() if len(groups) > 1 else ""

        elif intent == "data_freshness":
            parsed.action = "agent"
            parsed.args = "fyers freshness"

        elif intent == "data_update":
            parsed.action = "agent"
            parsed.args = "fyers update"

    def _fallback(self, command: str) -> ParsedCommand:
        """Treat unrecognized commands as goal creation."""
        return ParsedCommand(
            raw=command,
            intent="create_goal",
            confidence=0.40,
            target=command,
            fallback=True,
            reason="no pattern matched — treated as goal",
        )

    def _resolve_app_name(self, name: str) -> str:
        """Resolve an app alias to its canonical name."""
        name_lower = name.lower().strip()
        # 1. Exact canonical match first
        canonical = {
            "vs code": "VS Code", "visual studio code": "VS Code",
            "vscode": "VS Code",
            "claude code": "Claude Code", "claude": "Claude Code", "cc": "Claude Code",
            "windows terminal": "Windows Terminal", "terminal": "Windows Terminal", "wt": "Windows Terminal",
            "file explorer": "File Explorer", "explorer": "File Explorer", "files": "File Explorer",
            "task scheduler": "Task Scheduler", "schtasks": "Task Scheduler",
            "power shell": "PowerShell", "powershell": "PowerShell", "ps": "PowerShell",
            "google chrome": "Chrome", "chrome": "Chrome", "browser": "Chrome",
            "obsidian": "Obsidian",
            "git": "Git", "git bash": "Git",
            "python": "Python",
            "jarvis": "JARVIS",
            "notepad": "Notepad",
        }
        if name_lower in canonical:
            return canonical[name_lower]
        # 2. Substring: prefer exact prefix/suffix match over partial
        for app in self._known_apps:
            if name_lower == app.lower():
                return app
        for app in self._known_apps:
            if app.lower().startswith(name_lower) or name_lower.startswith(app.lower()):
                return app
        # 3. Fallback: longest substring
        candidates = [(app, len(app)) for app in self._known_apps
                      if name_lower in app.lower() or app.lower() in name_lower]
        if candidates:
            candidates.sort(key=lambda x: -x[1])
            return candidates[0][0]
        return name

    def _extract_deadline(self, text: str) -> str:
        """Extract a deadline string from text."""
        for pattern, _ in _DEADLINE_PATTERNS:
            m = pattern.search(text)
            if m:
                return m.group(0).strip()
        return ""
