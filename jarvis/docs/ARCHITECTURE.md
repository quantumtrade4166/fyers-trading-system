# JARVIS — Phase 0: Architecture & Discovery
> Generated: 2026-09-24
> Status: APPROVED — Pending user sign-off before Phase 1 implementation
> Supersedes: None (first architecture document)
> Integrates: JARVIS Master Engineering Specification + Desktop Control Addendum

---

## 0. Executive Summary

JARVIS is a production-grade personal AI operating system. This document captures the complete Phase 0
discovery output: repository assessment, proposed architecture, requirements traceability matrix (including
the Desktop Control Addendum), dependency map, risk register, and phased implementation plan.

**Key decisions (user-approved):**
- Host: Local PC only (Phase 0–14), VPS migration is a config flip (Phase 15)
- Interface: iPhone voice-first + web dashboard (Phase 11 for iOS; Phase 1 provides REST API)
- Stack: Python 3.12 + FastAPI + SQLite (WAL mode)
- Phase 1 scope: Foundation only — repo layout, config, DB schema, logging, 200+ tests, docs

---

## 1. Repository Assessment

### 1.1 What exists at G:\fyers_data_pipeline\

| Attribute | Value |
|---|---|
| Python files | 738 |
| Lines of Python | ~150,000 |
| Test files (real suites) | ~30 (deployment + live_trading_options + options/breeze) |
| Python version | 3.12.1 |
| Venv packages | 275 |
| Active branch | `main` |
| Git remote | `quantumtrade4166/fyers-trading-system` (private) |

### 1.2 Existing subsystems

| Subsystem | Location | Status | Reuse for JARVIS? |
|---|---|---|---|
| Trading dashboard (FastAPI) | `deployment/main.py` | LIVE on VPS | Pattern only (separate process) |
| Live options engines | `live_trading_options/` | LIVE | None (untouchable) |
| Data pipeline | `downloader/`, `options/` | Complete | None |
| Backtesting | `backtesting/` | Complete | None |
| DualMom paper + live | `deployment/dualmom_*.py` | LIVE | None |
| Options pipeline (NIFTY) | `options/` | Daily | None |
| Breeze options loader | `options/breeze/` | On-demand | None |
| Strangle system | `strangle_system/`, `live_trading_options/strangle_strategy/` | LIVE | None |
| BTC DN engine | `live_trading_options/delta_btc/` | Paper | None |
| Forex/XAUUSD | `forex/` | Backtest | None |
| ETF data | `etf/` | Partial | None |
| Nifty 500 daily data | `Nifty 500 Daily Data/` | Downloaded | None |

### 1.3 What JARVIS can reuse (without touching trading code)

| Asset | Why reusable |
|---|---|
| FastAPI + uvicorn pattern | Proven server pattern, same venv |
| python-dotenv config loading | Same pattern, separate `.env` |
| APScheduler 3.11.2 | Scheduled health checks, reminders |
| Structured logging pattern | From existing engines |
| `core/singleton.py` | Single-instance guard pattern |
| `tools/rebuild_desktop_index.py` | Claude session discovery logic |
| Windows Task Scheduler | Auto-start JARVIS at boot |
| `fyers_data_pipeline` venv | Shared Python 3.12 environment |
| Git workflow | Same repo, separate `jarvis/` directory |

### 1.4 What JARVIS must build from scratch

| Component | Reason |
|---|---|
| `jarvis/` directory + all source files | New namespace |
| SQLite database + schema | No DB exists in the project |
| Command ledger (immutable) | No command history system |
| Memory hierarchy (16 domains) | No structured memory beyond Obsidian vault |
| Goal + task system | No goal tracking beyond vault notes |
| Project registry | Needs to map to existing + future projects |
| Claude Code session registry | Needs desktop session discovery |
| Agent runtime | No agent orchestration layer |
| Desktop control layer | No desktop automation |
| Self-diagnostics | No system health monitoring |
| Security vault | No encrypted secret storage |
| Permission system | No fine-grained permission model |
| iOS app | New (Phase 11) |
| Voice integration | New (Phase 10) |
| Telephony | New (Phase 12) |

---

## 2. Proposed Architecture

### 2.1 Directory Layout

```
G:\fyers_data_pipeline\
├── jarvis\                          ← JARVIS namespace (new)
│   ├── __init__.py
│   ├── main.py                      # FastAPI app (port 8001)
│   ├── config.py                    # Central config + UTC enforcement
│   ├── constants.py                 # Enums: permissions, statuses, agent IDs
│   ├── exceptions.py                # JARVIS exception hierarchy
│   ├── logging_config.py            # Structured JSON logging
│   ├── CONSTITUTION.md              # JARVIS Constitution (immutable)
│   │
│   ├── core\                        # JARVIS Core
│   │   ├── orchestrator.py          # Main entry: intent → agent → result → record
│   │   ├── intent_engine.py         # Intent classification + entity extraction
│   │   ├── context_manager.py       # Resolves references ("that project", "the dashboard")
│   │   ├── conversation.py          # Conversation state + turn management
│   │   ├── tool_registry.py         # All available tools/integrations
│   │   └── agent_registry.py        # Claude Code, research, analytics, desktop agents
│   │
│   ├── memory\                      # 16-domain memory hierarchy
│   │   ├── database.py              # SQLite connection + migrations
│   │   ├── command_ledger.py        # Immutable command history
│   │   ├── project_memory.py        # Per-project context blobs
│   │   ├── goals.py                 # Goals, milestones, deadlines
│   │   ├── tasks.py                 # Tasks, dependencies, priorities
│   │   ├── decisions.py             # Decision memory (why + evidence)
│   │   ├── timeline.py              # Unified chronological event stream
│   │   ├── relationships.py         # People/projects/goals graph
│   │   ├── preferences.py           # Learned preferences + operating principles
│   │   ├── commitments.py           # User promises + deadlines
│   │   ├── health.py                # JARVIS self-health history
│   │   ├── ideas.py                 # Idea incubator
│   │   └── agents_history.py        # Agent action history + accountability
│   │
│   ├── agents\                      # Agent Runtime
│   │   ├── base.py                  # Agent base class (identity, perms, tools, stop conditions)
│   │   ├── claude_code.py           # Claude Code delegation + session search
│   │   ├── research.py              # Web research agent
│   │   ├── analytics.py             # Trading analysis agent
│   │   ├── trading.py               # Trading system integration (read-only)
│   │   └── desktop.py               # Desktop control agent (app launch, shell, GUI)
│   │
│   ├── desktop\                     # Desktop Control Layer (ADDENDUM)
│   │   ├── __init__.py
│   │   ├── app_registry.py          # Application registry (name, path, aliases, perms)
│   │   ├── project_map.py           # Project → application mapping
│   │   ├── launcher.py              # Application launching + verification
│   │   ├── shell.py                 # Authorized shell execution
│   │   ├── gui.py                   # GUI automation (pywin32/UIA fallback)
│   │   ├── state.py                 # Desktop state awareness (running apps, services)
│   │   ├── device.py                # Device identity + authentication
│   │   └── remote.py                # Remote device support (future)
│   │
│   ├── integrations\                # External system integrations
│   │   ├── __init__.py
│   │   ├── obsidian.py              # Obsidian vault indexing + search
│   │   ├── github.py                # GitHub API (repo listing, commits)
│   │   ├── openai_voice.py          # OpenAI Realtime Voice (Phase 10 stub)
│   │   ├── telephony.py             # Outbound calls (Phase 12 stub)
│   │   ├── gmail.py                 # Gmail (Phase 9 stub)
│   │   ├── calendar.py              # Calendar (Phase 9 stub)
│   │   ├── filesystem.py            # Local file operations
│   │   └── trading_systems.py       # Read-only trading system queries
│   │
│   ├── api\                         # REST + WebSocket API
│   │   ├── __init__.py
│   │   ├── commands.py              # POST /api/command — user intent entry
│   │   ├── memory.py                # GET /api/memory/*
│   │   ├── goals.py                 # CRUD /api/goals/*
│   │   ├── tasks.py                 # CRUD /api/tasks/*
│   │   ├── projects.py              # GET /api/projects/*
│   │   ├── desktop.py               # Desktop control API (Phase 8 surface)
│   │   ├── health.py                # GET /api/health — self-diagnostics
│   │   ├── websocket.py             # WS /ws — real-time push
│   │   └── voice_stub.py            # POST /api/voice/transcript (Phase 10 stub)
│   │
│   ├── security\                    # Security layer
│   │   ├── __init__.py
│   │   ├── vault.py                 # AES-256-GCM encrypted secret storage
│   │   ├── permissions.py           # Permission levels + check decorator
│   │   ├── audit.py                 # Append-only audit trail (hash-chained)
│   │   └── kill_switch.py           # Emergency stop
│   │
│   ├── diagnostics\                 # Self-diagnostics + reliability
│   │   ├── __init__.py
│   │   ├── self_check.py            # Full system diagnostics
│   │   ├── integrity.py             # Memory integrity checks
│   │   └── backups.py               # Backup + restore
│   │
│   ├── tests\                       # 200+ tests
│   │   ├── conftest.py
│   │   ├── test_command_ledger.py
│   │   ├── test_memory_retrieval.py
│   │   ├── test_goals.py
│   │   ├── test_tasks.py
│   │   ├── test_intent.py
│   │   ├── test_permissions.py
│   │   ├── test_audit.py
│   │   ├── test_desktop_launcher.py
│   │   ├── test_desktop_app_registry.py
│   │   ├── test_desktop_shell.py
│   │   ├── test_desktop_state.py
│   │   ├── test_desktop_verification.py
│   │   └── ...                      # Additional test files per phase
│   │
│   ├── scripts\                     # One-time + maintenance scripts
│   │   ├── init_db.py               # Create all tables (idempotent)
│   │   ├── seed_apps.py             # Seed default application registry
│   │   ├── seed_projects.py         # Import projects from vault
│   │   └── migrate.py               # Schema migrations
│   │
│   ├── docs\                        # Documentation
│   │   ├── ARCHITECTURE.md          # This file
│   │   ├── API.md                   # REST + WebSocket API reference
│   │   ├── DEPLOYMENT.md            # Local PC + VPS deployment runbook
│   │   ├── RUNBOOK.md               # Operations runbook
│   │   └── DESKTOP_CONTROL.md       # Desktop control addendum detail
│   │
│   ├── data\                        # Runtime data (gitignored)
│   │   └── jarvis.db                # SQLite database (WAL mode)
│   │
│   ├── logs\                        # JARVIS logs (gitignored)
│   │   ├── jarvis.log
│   │   ├── desktop_control.log
│   │   └── audit.log
│   │
│   ├── pyproject.toml               # Build + tool config
│   ├── requirements.txt             # JARVIS dependencies (additive)
│   └── .env.example                 # Template (no real secrets)
│
├── deployment\                      ← EXISTING — DO NOT TOUCH
├── live_trading_options\            ← EXISTING — DO NOT TOUCH
├── backtesting\                     ← EXISTING — DO NOT TOUCH
├── options\                         ← EXISTING — DO NOT TOUCH
├── ...                              ← All other existing directories unchanged
└── CLAUDE.md                        ← EXISTING — DO NOT TOUCH
```

### 2.2 Process Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  USER                                                        │
│  (voice / iPhone app / web dashboard / direct API)           │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  JARVIS FastAPI (port 8001)                                  │
│  ┌─────────────────────────────────────────────────────────┐│
│  │  API Layer                                              ││
│  │  POST /api/command  →  intent + context + permissions   ││
│  │  GET  /api/health   →  self-diagnostics                 ││
│  │  WS   /ws           →  real-time push (iOS/dashboard)   ││
│  └─────────────────────────────────────────────────────────┘│
│                        │                                     │
│  ┌─────────────────────▼───────────────────────────────────┐│
│  │  JARVIS Core                                            ││
│  │  orchestrator → intent_engine → context_manager         ││
│  │  → tool_registry → agent_registry                       ││
│  └─────────────────────┬───────────────────────────────────┘│
│                        │                                     │
│  ┌─────────────────────▼───────────────────────────────────┐│
│  │  Agent Runtime                                          ││
│  │  ┌────────────┐  ┌──────────────┐  ┌───────────────┐  ││
│  │  │ Claude Code│  │   Desktop    │  │   Research    │  ││
│  │  │ Agent      │  │   Agent      │  │   Agent       │  ││
│  │  └────────────┘  └──────────────┘  └───────────────┘  ││
│  └─────────────────────┬───────────────────────────────────┘│
│                        │                                     │
│  ┌─────────────────────▼───────────────────────────────────┐│
│  │  Desktop Control Layer  (ADDENDUM)                      ││
│  │  app_registry → launcher → shell → gui → state          ││
│  │  All actions: permission check → execute → verify →     ││
│  │  record in audit trail                                   ││
│  └─────────────────────┬───────────────────────────────────┘│
│                        │                                     │
│  ┌─────────────────────▼───────────────────────────────────┐│
│  │  Memory + Knowledge                                     ││
│  │  SQLite: command_ledger, goals, tasks, decisions,       ││
│  │  timeline, preferences, audit_trail                     ││
│  │  Obsidian: vault indexing + search (Phase 8)            ││
│  └─────────────────────┬───────────────────────────────────┘│
│                        │                                     │
│  ┌─────────────────────▼───────────────────────────────────┐│
│  │  Security                                               ││
│  │  vault (secrets) → permissions → audit (immutable)      ││
│  │  → kill_switch                                          ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
        ▲
        │ localhost:8001
        │
   ┌────┴──────────────────────────┐
   │  iPhone App (Phase 11)         │  Web Dashboard (Phase 1+)
   │  Voice-first + controls        │  Read-only view of JARVIS state
   └────────────────────────────────┘
```

### 2.3 Key Architectural Principles

| Principle | Implementation |
|---|---|
| **Local-first** | JARVIS runs on local PC. VPS migration = config change, not rewrite. |
| **Zero touch to trading** | Separate process (port 8001). Never imports deployment/, live_trading_options/, backtesting/. |
| **UTC everywhere** | All timestamps stored in UTC. IST conversion only at display boundary. |
| **Source of truth** | JARVIS memory indexes real sources (Git, Obsidian, Claude sessions, trading records). It never replaces them. |
| **Immutable history** | Command ledger and audit trail are append-only. No UPDATE, no DELETE. |
| **Verify, don't assume** | Desktop actions verify post-condition. Agent actions verify against source. |
| **Permission by default** | All actions require permission check. Human override always wins. |
| **Fail loudly** | Auth failures, missing data, dead sessions → raise, never silently degrade. |
| **Bounded retry** | Max 3 retries with backoff. Never uncontrolled loops. |
| **Sandbox first** | Important autonomous work → branch/sandbox → verify → approve → production. |

---

## 3. Requirements Traceability Matrix (Complete — Master Spec + Desktop Addendum)

### 3.1 Legend

| Column | Meaning |
|---|---|
| Req # | Requirement identifier |
| Phase | When implemented |
| Component | Primary implementing module |
| Dependencies | Prerequisites |
| Tests | Test coverage plan |
| Status | ⬜ Not started / 🔄 In progress / ✅ Complete |

### 3.2 Core System Requirements

| Req # | Description | Phase | Component | Dependencies | Tests | Status |
|---|---|---|---|---|---|---|
| 0 | Senior architect role — this document | 0 | ARCHITECTURE.md | — | Document review | ✅ |
| 1 | Inspect before building (discovery first) | 0 | This document | — | — | ✅ |
| 2 | Core philosophy: source of truth, never fabricate | 2 | orchestrator.py, project_memory.py | Phase 1 | test_source_truth | ⬜ |
| 3 | High-level architecture | 0 | Directory layout §2.1 | — | — | ✅ |
| 4 | Voice architecture (OpenAI Realtime, model-independent) | 10 | openai_voice.py (stub in Phase 1) | OpenAI API key | Phase 10 | ⬜ |
| 5 | iPhone interface (voice-first, Siri, CarPlay, notifications) | 11 | iOS app (separate project) | Phase 1 REST API | Phase 11 | ⬜ |
| 6 | Outbound phone calls (telephony) | 12 | telephony.py (stub) | Twilio/similar | Phase 12 | ⬜ |
| 7 | JARVIS Core: intent, routing, tool, agent, permission, memory, verify, audit | 2 | orchestrator.py, intent_engine.py, context_manager.py, tool_registry.py, agent_registry.py | Phase 1 | test_orchestrator, test_intent, test_context | ⬜ |
| 8 | Memory hierarchy (16 domains) | 3 | memory/*.py | Phase 1 DB | test_memory_domains | ⬜ |
| 9 | Permanent command memory (immutable ledger) | 3 | command_ledger.py | Phase 1 DB | test_command_ledger | ⬜ |
| 10 | Memory integrity checks | 14 | diagnostics/integrity.py | Phase 3 | test_integrity | ⬜ |
| 11 | Memory correction (distinguish immutable vs mutable) | 3 | memory/*.py (soft-delete + versioning) | Phase 3 | test_memory_correction | ⬜ |
| 12 | Source of truth (retrieve from real sources) | 3–9 | All integrations | Per integration | test_source_truth | ⬜ |
| 13 | Project identity (name, repo, branch, Claude sessions, Obsidian folder) | 3 | project_memory.py | Phase 1 | test_project_identity | ⬜ |
| 14 | Check-before-acting (inspect source before modifying) | 2 | orchestrator.py pre-flight | Phase 1 | test_check_before_acting | ⬜ |
| 15 | Claude Code integration (project registry, session search) | 5 | agents/claude_code.py | Claude Code CLI | test_claude_integration | ⬜ |
| 16 | Claude Code execution workflow (inspect → delegate → monitor → verify → record) | 5 | agents/claude_code.py | Phase 5 | test_claude_workflow | ⬜ |
| 17 | Goal system (creation, deadlines, milestones, success criteria) | 4 | goals.py | Phase 1 DB | test_goals | ⬜ |
| 18 | Task/project system (decomposition, dependencies, priorities, states) | 4 | tasks.py | Phase 1 DB | test_tasks | ⬜ |
| 19 | Goal conflict detection | 4 | goals.py | Phase 4 | test_goal_conflicts | ⬜ |
| 20 | Resource awareness (time, compute, API usage) | 4 | orchestrator.py | Phase 4 | test_resource_awareness | ⬜ |
| 21 | Commitment memory (promises tracked separately) | 4 | commitments.py | Phase 1 DB | test_commitments | ⬜ |
| 22 | Waiting-state intelligence | 4 | tasks.py | Phase 4 | test_waiting_states | ⬜ |
| 23 | Priority engine | 4 | tasks.py | Phase 4 | test_priority_engine | ⬜ |
| 24 | Chief-of-staff behavior ("what should I focus on?") | 4 | commands.py (intent patterns) | Phase 4 | test_chief_of_staff | ⬜ |
| 25 | Context awareness (driving, working, busy) | 2 | context_manager.py | Phase 2 | test_context_awareness | ⬜ |
| 26 | Interruption/follow-up (retain previous task) | 2 | conversation.py | Phase 2 | test_interruption | ⬜ |
| 27 | Long-running jobs (status tracking after disconnect) | 6 | agents/base.py | Phase 6 | test_long_running | ⬜ |
| 28 | Failure recovery (bounded: investigate → retry → verify → escalate) | 6 | agents/base.py + retry decorators | Phase 6 | test_failure_recovery | ⬜ |
| 29 | Autonomous missions (objective, plan, permissions, stop conditions) | 6 | agents/base.py | Phase 6 | test_autonomous_missions | ⬜ |
| 30 | Autonomous agents (identity, permissions, tools, limits, success criteria) | 6 | agents/*.py | Phase 6 | test_agent_lifecycle | ⬜ |
| 31 | Agent handoff (research → code → test → verify → JARVIS) | 6 | agents/base.py | Phase 6 | test_agent_handoff | ⬜ |
| 32 | Agent accountability (every action attributed) | 7 | audit.py | Phase 7 | test_agent_accountability | ⬜ |
| 33 | Security vault (API keys, broker creds, encrypted) | 7 | security/vault.py | cryptography lib | test_vault | ⬜ |
| 34 | Permissions (L0–L4, risk classification) | 7 | security/permissions.py | Phase 1 | test_permissions | ⬜ |
| 35 | Permission expiry (temporary grants auto-revoke) | 7 | security/permissions.py | Phase 7 | test_permission_expiry | ⬜ |
| 36 | Human override (explicit user instruction > learned preference) | 2 | orchestrator.py | Phase 2 | test_human_override | ⬜ |
| 37 | Emergency kill switch | 7 | security/kill_switch.py | Phase 7 | test_kill_switch | ⬜ |
| 38 | Sandbox mode (branch → modify → test → approve → production) | 6 | agents/base.py | Phase 6 | test_sandbox | ⬜ |
| 39 | Simulation/preflight (explain → request approval) | 2 | orchestrator.py | Phase 2 | test_preflight | ⬜ |
| 40 | Evidence bundles (files changed, tests, commits, verification) | 7 | audit.py | Phase 7 | test_evidence_bundles | ⬜ |
| 41 | Complete audit trail (command → interpretation → decision → action → result) | 7 | audit.py | Phase 1 | test_audit_trail | ⬜ |
| 42 | Source transparency (current/historical/inferred/uncertain) | 2 | orchestrator.py | Phase 2 | test_source_transparency | ⬜ |
| 43 | Confidence system (verified/current/historical/inferred/uncertain) | 15 | decisions.py | Phase 15 | test_confidence | ⬜ |
| 44 | EXCLUDED: standalone Life Dashboard | — | — | User decision | — | N/A |
| 45 | EXCLUDED: automatic model provider failover | — | — | User decision | — | N/A |
| 46 | Obsidian integration (index, search, project association, knowledge graph) | 8 | integrations/obsidian.py | Phase 8 | test_obsidian_integration | ⬜ |
| 47 | Cross-source reasoning (combine Claude/Git/Gmail/Calendar/etc.) | 8 | orchestrator.py | Phase 8 | test_cross_source | ⬜ |
| 48 | Unified timeline (chronological events across all sources) | 3 | timeline.py | Phase 1 DB | test_timeline | ⬜ |
| 49 | Historical time machine (reconstruct what was true at time X) | 3 | timeline.py | Phase 3 | test_time_machine | ⬜ |
| 50 | Decision memory (decision, date, reason, alternatives, evidence) | 3 | decisions.py | Phase 1 DB | test_decisions | ⬜ |
| 51 | Relationship/context graph (people → projects → goals → tasks) | 8 | relationships.py | Phase 8 | test_relationships | ⬜ |
| 52 | Personal search engine (cross-knowledge search) | 8 | obsidian.py + FTS | Phase 8 | test_search | ⬜ |
| 53 | Second brain (Obsidian + JARVIS as long-term knowledge) | 8 | obsidian.py + memory/ | Phase 8 | test_second_brain | ⬜ |
| 54 | Idea incubator (preserve casual mentions) | 15 | ideas.py | Phase 15 | test_ideas | ⬜ |
| 55 | Cross-project learning (surface solutions from other projects) | 15 | orchestrator.py | Phase 15 | test_cross_project | ⬜ |
| 56 | Automation suggestions (detect repetitive workflows) | 15 | self_check.py | Phase 15 | test_automation_suggestions | ⬜ |
| 57 | Auto documentation (update docs on major changes) | 15 | Future agent | Phase 15 | — | ⬜ |
| 58 | Project health (healthy/at risk/blocked/stale/completed) | 4 | project_memory.py | Phase 4 | test_project_health | ⬜ |
| 59 | EXCLUDED: provider failover | — | — | User decision | — | N/A |
| 60 | Self-diagnostics (check all subsystems, safe recovery) | 14 | diagnostics/self_check.py | Phase 1 | test_self_diagnostics | ⬜ |
| 61 | Daily/weekly/monthly intelligence (briefings, reviews) | 13 | commands.py (intent) | Phase 13 | test_briefings | ⬜ |
| 62 | Attention management (urgent/important/normal/low) | 13 | orchestrator.py | Phase 13 | test_attention | ⬜ |
| 63 | Meeting intelligence (pre/post meeting context) | Future | Future | Future | — | ⬜ |
| 64 | People/relationship context | 8 | relationships.py | Phase 8 | test_relationships | ⬜ |
| 65 | Trading intelligence (P&L, strategy performance, never fabricate) | 9 | agents/trading.py | Phase 9 | test_trading_intel | ⬜ |
| 66 | "What am I forgetting?" (deadlines, commitments, blockers) | 13 | orchestrator.py | Phase 13 | test_forgetting | ⬜ |
| 67 | Daily context (understand current work without restating) | 2 | context_manager.py | Phase 2 | test_daily_context | ⬜ |
| 68 | Personal operating principles (durable preferences) | 2 | preferences.py | Phase 2 | test_preferences | ⬜ |
| 69 | JARVIS Constitution (15 principles, immutable) | 1 | CONSTITUTION.md | Phase 1 | test_constitution_immutable | ⬜ |
| 70 | Self-improvement loop (learn from outcomes, distinguish from rules) | 15 | self_check.py | Phase 15 | test_self_improvement | ⬜ |
| 71 | Goal conflict detection (duplicate of 19) | 4 | goals.py | Phase 4 | test_goal_conflicts | ⬜ |
| 72 | Resource awareness (duplicate of 20) | 4 | orchestrator.py | Phase 4 | test_resource_awareness | ⬜ |
| 73 | Commitment memory (duplicate of 21) | 4 | commitments.py | Phase 4 | test_commitments | ⬜ |
| 74 | Waiting-state intelligence (duplicate of 22) | 4 | tasks.py | Phase 4 | test_waiting_states | ⬜ |
| 75 | Autonomous retry policy (bounded) | 6 | agents/base.py | Phase 6 | test_retry_policy | ⬜ |
| 76 | Safe sandbox | 6 | agents/base.py | Phase 6 | test_sandbox | ⬜ |
| 77 | Evidence bundle (duplicate of 40) | 7 | audit.py | Phase 7 | test_evidence_bundles | ⬜ |
| 78 | Permission expiry (duplicate of 35) | 7 | permissions.py | Phase 7 | test_permission_expiry | ⬜ |
| 79 | Temporary missions (objective, budget, time limit, stop conditions) | 6 | agents/base.py | Phase 6 | test_missions | ⬜ |
| 80 | Personal success criteria (explicit "done" definition) | 4 | goals.py | Phase 4 | test_success_criteria | ⬜ |
| 81 | Historical "why" (reconstruct from actual history) | 3 | decisions.py + timeline.py | Phase 3 | test_historical_why | ⬜ |
| 82 | Personal pattern detection (recurring corrections, delays, bottlenecks) | 15 | self_check.py | Phase 15 | test_pattern_detection | ⬜ |
| 83 | Anomaly detection (unusual events, flag for inspection) | 15 | self_check.py | Phase 15 | test_anomaly_detection | ⬜ |
| 84 | Memory integrity (missing/duplicate/invalid events) | 14 | diagnostics/integrity.py | Phase 14 | test_memory_integrity | ⬜ |
| 85 | Complete audit trail (duplicate of 41) | 7 | audit.py | Phase 7 | test_audit_trail | ⬜ |
| 86 | Human override always wins | 2 | orchestrator.py | Phase 2 | test_human_override | ⬜ |
| 87 | Memory correction (facts/preferences/outdated info, preserve immutable history) | 3 | memory/*.py | Phase 3 | test_memory_correction | ⬜ |
| 88 | JARVIS health history (recurring failures tracked) | 14 | memory/health.py | Phase 14 | test_health_history | ⬜ |
| 89 | Predictive assistance (upcoming issues, not consequential without auth) | 13 | orchestrator.py | Phase 13 | test_predictive | ⬜ |
| 90 | Intent understanding (meaning + references, not keywords) | 2 | intent_engine.py | Phase 2 | test_intent | ⬜ |
| 91 | Contextual memory compression (raw → summaries → indexes, never destroy originals) | 15 | Future | Phase 15 | — | ⬜ |
| 92 | Knowledge confidence (current/historical/authoritative/inferred/uncertain) | 15 | decisions.py | Phase 15 | test_confidence | ⬜ |
| 93 | Cross-project learning (surface relevant solutions) | 15 | orchestrator.py | Phase 15 | test_cross_project | ⬜ |
| 94 | Personal automation suggestions (suggest, don't silently automate) | 15 | self_check.py | Phase 15 | test_automation_suggestions | ⬜ |
| 95 | Proactive knowledge linking | Future | Future | Future | — | ⬜ |
| 96 | Natural agent handoffs | 6 | agents/base.py | Phase 6 | test_agent_handoff | ⬜ |
| 97 | Agent accountability (duplicate of 32) | 7 | audit.py | Phase 7 | test_agent_accountability | ⬜ |
| 98 | Personal operating principles (duplicate of 68) | 2 | preferences.py | Phase 2 | test_preferences | ⬜ |
| 99 | Milestone memory (idea → decision → implementation → launch) | 4 | goals.py | Phase 4 | test_milestones | ⬜ |
| 100 | Self-reflection (evaluate own performance, recommend improvements) | 15 | self_check.py | Phase 15 | test_self_reflection | ⬜ |
| 101 | Requirements traceability (living document, every capability mapped) | 1 | This document + traceability.py | Phase 1 | test_traceability | ⬜ |
| 102 | Testing (200+ tests covering all subsystems) | 1 | tests/ | Phase 1 | pytest suite | ⬜ |
| 103 | Observability (structured logs, metrics, traces, health status) | 1 | logging_config.py | Phase 1 | test_observability | ⬜ |
| 104 | Backup/DR (encrypted backups, restore, integrity verification) | 14 | diagnostics/backups.py | Phase 14 | test_backup_restore | ⬜ |
| 105 | Cost intelligence (track AI/API/cloud/telephony costs) | Future | Future | Future | — | ⬜ |
| 106 | Model independence (not hard-coded to one provider) | 2 | agents/base.py (provider-agnostic interface) | Phase 2 | test_model_independence | ⬜ |
| 107 | Self-diagnostic command ("JARVIS, check yourself.") | 14 | diagnostics/self_check.py | Phase 14 | test_self_diagnostic_command | ⬜ |
| 108 | Final quality standard (implemented + integrated + tested + documented + verified) | All | Per-phase acceptance criteria | — | Per-phase | ⬜ |
| 109 | Implementation phases (controlled, incremental, no destructive changes) | All | This roadmap | — | — | ✅ |
| 110 | Final acceptance criteria (natural voice-style interactions) | All | End-to-end integration tests | All phases | test_acceptance | ⬜ |
| 111 | Final objective (persistent personal assistant, permanent auditable memory) | All | All phases | — | — | ✅ |

### 3.3 Desktop Control Addendum Requirements

| Req # | Description | Phase | Component | Dependencies | Tests | Status |
|---|---|---|---|---|---|---|
| DC-1 | Desktop Control: open/close/focus/minimize/maximize applications | 8 | desktop/launcher.py | pywin32 | test_launch_app, test_verify_window | ⬜ |
| DC-2 | Desktop Control: open files, folders, project directories | 8 | desktop/launcher.py | os.startfile / shell | test_open_path | ⬜ |
| DC-3 | Desktop Control: open URLs in browser, specific pages | 8 | desktop/launcher.py | webbrowser stdlib | test_open_url | ⬜ |
| DC-4 | Desktop Control: launch terminal/shell environments | 8 | desktop/launcher.py | subprocess | test_launch_terminal | ⬜ |
| DC-5 | Desktop Control: execute approved shell commands (authorized only) | 8 | desktop/shell.py | subprocess, permissions | test_shell_exec, test_shell_permissions | ⬜ |
| DC-6 | Desktop Control: open development environments (VS Code, IDEs, Git clients) | 8 | desktop/launcher.py | app_registry.py | test_launch_ide | ⬜ |
| DC-7 | Desktop Control: open Claude Code with correct project/workspace | 8 | desktop/launcher.py + agents/claude_code.py | app_registry, project_map | test_launch_claude_code | ⬜ |
| DC-8 | Desktop Control: open Obsidian, navigate to notes | 8 | desktop/launcher.py | app_registry | test_launch_obsidian | ⬜ |
| DC-9 | Desktop Control: open/restart/stop/monitor local services | 8 | desktop/launcher.py + desktop/state.py | shell, permissions | test_service_control | ⬜ |
| DC-10 | Desktop Control: GUI automation (click, type, switch windows) — fallback only | 8 | desktop/gui.py | pywin32, UIAutomation | test_gui_click, test_gui_type | ⬜ |
| DC-11 | Natural-language application control ("open my trading project") | 8 | intent_engine.py + desktop/launcher.py | app_registry, project_map | test_natural_language_control | ⬜ |
| DC-12 | Application Registry (name, aliases, path, OS, perms, allowed actions, health) | 1 | desktop/app_registry.py | Phase 1 DB | test_app_registry | ⬜ |
| DC-13 | Project-to-Application Mapping (project → apps, workflows) | 1 | desktop/project_map.py | Phase 1 DB | test_project_map | ⬜ |
| DC-14 | Claude Code desktop integration (open, resume, delegate, verify, record) | 8 | desktop/launcher.py + agents/claude_code.py | DC-7, Req 15 | test_claude_desktop_integration | ⬜ |
| DC-15 | Terminal/Shell Control with full audit (command, cwd, exit code, duration) | 8 | desktop/shell.py | DC-5, audit.py | test_shell_audit | ⬜ |
| DC-16 | GUI Automation — prefer APIs/CLIs, coordinate-based is fallback | 8 | desktop/gui.py | DC-10 | test_gui_fallback | ⬜ |
| DC-17 | Permission + Risk Model (low/medium/high, human override) | 7 | security/permissions.py + desktop/ | DC-1–DC-16 | test_desktop_permissions | ⬜ |
| DC-18 | Verification (every desktop action verified post-condition) | 8 | desktop/launcher.py (verify methods) | DC-1–DC-16 | test_desktop_verification | ⬜ |
| DC-19 | Desktop State Awareness (running apps, windows, services — minimal, not surveillance) | 8 | desktop/state.py | pywin32, permissions | test_desktop_state | ⬜ |
| DC-20 | Multi-device support (device identity, authorization boundary) | 8 | desktop/device.py | Phase 8 | test_device_registration | ⬜ |
| DC-21 | Desktop Security (device auth, command auth, least-privilege, credential isolation) | 7 | security/vault.py + desktop/device.py | Phase 7 | test_desktop_security | ⬜ |
| DC-22 | Desktop Audit Trail (command, device, app, action, result, verification — immutable) | 7 | security/audit.py | audit.py | test_desktop_audit | ⬜ |
| DC-23 | Voice Control of Desktop (full voice operation of low-risk desktop commands) | 10 | openai_voice.py + desktop/ | DC-1–DC-18 | test_voice_desktop | ⬜ |
| DC-24 | Remote Desktop Support (future: secondary machine, cloud VM, AWS) | 9 | desktop/remote.py | DC-20 | test_remote_desktop | ⬜ |
| DC-25 | Failure Recovery (detect → determine cause → bounded retry → verify → report) | 8 | desktop/launcher.py + agents/base.py | DC-1–DC-18 | test_desktop_recovery | ⬜ |
| DC-26 | Desktop Self-Diagnostics (check desktop agent, apps, shell, GUI, permissions) | 14 | diagnostics/self_check.py + desktop/ | DC-1–DC-25 | test_desktop_diagnostics | ⬜ |
| DC-27 | Human Override (explicit instruction overrides learned preference for desktop actions) | 7 | security/permissions.py | DC-17 | test_desktop_override | ⬜ |

### 3.4 Requirement Count Summary

| Category | Count | Phased |
|---|---|---|
| Master spec requirements | 111 | Phases 0–15 |
| Desktop Control Addendum | 27 (DC-1 through DC-27) | Phases 1, 7, 8, 9, 10, 14 |
| **Total unique requirements** | **~138** | — |
| Phase 1 (Foundation) | 8 | 1 |
| Phase 2 (JARVIS Core) | 12 | 2 |
| Phase 3 (Memory) | 10 | 3 |
| Phase 4 (Goals/Tasks) | 13 | 4 |
| Phase 5 (Claude Code) | 2 | 5 |
| Phase 6 (Agent Runtime) | 10 | 6 |
| Phase 7 (Security + Desktop Security) | 8 | 7 |
| Phase 8 (Knowledge + Desktop Control) | 13 | 8 |
| Phase 9 (External + Remote Desktop) | 2 | 9 |
| Phase 10 (Voice + Desktop Voice) | 3 | 10 |
| Phase 11 (Mobile/iOS) | 1 | 11 |
| Phase 12 (Telephony) | 1 | 12 |
| Phase 13 (Proactive) | 5 | 13 |
| Phase 14 (Reliability + Desktop Diagnostics) | 5 | 14 |
| Phase 15 (Advanced Intelligence) | 8 | 15 |
| Excluded by user | 2 | N/A |
| Future (not in current scope) | 3 | — |

---

## 4. Desktop Control Architecture (ADDENDUM Integration)

### 4.1 OS Support

| OS | Mechanism | Status | Phase |
|---|---|---|---|
| **Windows 10/11** (current) | pywin32 (win32api + win32gui + win32con) + UIAutomation (uiautomationcore) | ✅ Available | 8 |
| Windows (future) | Same + PowerShell remoting | — | Future |
| macOS (future) | AppleScript + osascript + Accessibility API | ❌ Not available yet | Future |
| Linux (future) | DBus + wmctrl + xdotool | ❌ Not available yet | Future |

**Current machine:** Windows (GMT+10). Phase 8 desktop control targets Windows only.
Remote desktop (DC-24) supports any OS via SSH + agent.

### 4.2 Desktop Control Technology Choices

| Need | Technology | Why |
|---|---|---|
| App launching | `subprocess.Popen` + `os.startfile` | Reliable, no coordinates |
| Window focus | `pywin32` (`SetForegroundWindow`) | Native Win32, no coordinates |
| App state | `pywin32.EnumWindows` + `GetWindowText` | Read-only, no injection |
| Shell commands | `subprocess.run` (capture, timeout, cwd) | Stdlib, auditable, bounded |
| GUI automation (fallback) | `uiautomationcore` (pip: `uiautomation`) | Accessibility API, not coordinate-based |
| GUI automation (last resort) | `pyautogui` (coordinates) | Explicitly flagged as fragile fallback |
| Process management | `psutil` | Cross-platform, reliable |
| Service control | Windows `sc.exe` + Task Scheduler CLI | Native, auditable |

### 4.3 Desktop Control Security Model

```
Permission Levels (integrated with existing JARVIS permissions):

L0 — Read-only (no confirmation needed if app authorized):
  • List running applications
  • Check service status
  • Read window titles
  • Open authorized applications

L1 — Low-risk modification (auto if authorized):
  • Open files/folders/URLs
  • Switch windows
  • Launch authorized terminal
  • Open authorized project in IDE

L2 — Medium-risk (require confirmation unless standing permission):
  • Execute shell commands (whitelisted)
  • Restart services
  • Install packages
  • Run tests/builds

L3 — High-risk (always require confirmation):
  • Delete files
  • Modify system config
  • Expose credentials
  • Execute unapproved commands
  • Place/modify live trades

L4 — Financial/destructive (always require explicit confirmation):
  • Live trading actions
  • Format disks
  • System-level changes
```

**Desktop Control Permission Flow:**
```
User command: "Open Claude Code and fix the trading dashboard"
  │
  ▼
Intent Engine → classify as: DESKTOP_LAUNCH + CLAUDE_DELEGATE
  │
  ▼
Permission Check:
  • "open Claude Code" → L0 (if Claude Code in app_registry, authorized)
  • "fix the trading dashboard" → L2 (delegate to Claude Code = medium risk)
  │
  ▼
If L0 auto-approved + L2 requires confirmation:
  → "Opening Claude Code. Should I ask it to fix the dashboard?"
  │
  ▼
User confirms:
  → Desktop agent: launch Claude Code
  → Verify: Claude Code window exists
  → Claude Code agent: delegate task
  → Verify: Claude Code inspected source, ran tests
  → Record: full audit trail
  → Report: "Done. Claude fixed the reconnect logic. Tests pass."
```

### 4.4 Application Registry Schema

```sql
CREATE TABLE app_registry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,           -- "Claude Code"
    aliases         TEXT,                    -- JSON: ["claude", "cc", "claude-code"]
    exe_path        TEXT,                    -- "C:\\Users\\PC\\AppData\\...\\Claude.exe"
    app_id          TEXT,                    -- Windows AppUserModelID or bundle ID
    os              TEXT DEFAULT 'windows',  -- windows / macos / linux
    install_location TEXT,
    associated_projects TEXT,                -- JSON: ["fyers_data_pipeline", "jarvis"]
    workflows       TEXT,                    -- JSON: ["development", "coding"]
    allowed_actions TEXT,                    -- JSON: ["launch", "focus", "close", "switch"]
    permission_level INTEGER DEFAULT 0,     -- 0=L0 through 4=L4
    gui_automation   INTEGER DEFAULT 0,      -- 0=no, 1=yes
    cli_automation   INTEGER DEFAULT 0,      -- 0=no, 1=yes
    api_available    INTEGER DEFAULT 0,      -- 0=no, 1=yes
    last_known_status TEXT DEFAULT 'unknown', -- running / not_running / unknown
    health_status    TEXT DEFAULT 'unknown',  -- healthy / unhealthy / unknown
    version          TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, os)
);
```

### 4.5 Desktop Control Audit Schema

```sql
CREATE TABLE desktop_actions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id          TEXT NOT NULL,         -- FK to command_ledger
    user_command        TEXT NOT NULL,         -- Exact original command
    timestamp           TEXT NOT NULL,          -- ISO 8601 UTC
    timezone            TEXT NOT NULL,          -- Local timezone (Australia/Sydney = GMT+10)
    device_id           TEXT NOT NULL,          -- Which device
    target_app          TEXT,                   -- Application name from registry
    target_project      TEXT,                   -- Resolved project
    target_session      TEXT,                   -- Claude Code session if applicable
    action_requested    TEXT NOT NULL,           -- "launch", "execute", "open", "close"
    action_performed    TEXT,                   -- What actually happened
    tool_used           TEXT,                   -- "subprocess", "pywin32", "uiautomation"
    permission_level    INTEGER NOT NULL,        -- 0–4
    permission_decision TEXT NOT NULL,           -- "auto_approved", "confirmed", "denied"
    confirmation_status TEXT,                   -- "not_required", "requested", "granted", "denied"
    result              TEXT,                   -- "success", "failure", "partial"
    verification_result TEXT,                   -- "verified", "failed", "not_applicable"
    error_info          TEXT,                   -- Error details if failed
    stdout              TEXT,                   -- Command output (sanitized)
    stderr              TEXT,                   -- Error output (sanitized)
    duration_ms         INTEGER,                -- Execution time
    follow_up_action    TEXT,                   -- Suggested next step
    related_goal_id     TEXT,                   -- FK to goals
    related_task_id     TEXT,                   -- FK to tasks
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
-- NO UPDATE, NO DELETE — append-only
```

### 4.6 Desktop State Schema (lightweight)

```sql
CREATE TABLE desktop_state (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_time   TEXT NOT NULL,              -- ISO 8601 UTC
    device_id       TEXT NOT NULL,
    running_processes TEXT,                     -- JSON: [{name, pid, title, app_id}]
    active_window    TEXT,                      -- Current foreground window title
    running_services TEXT,                      -- JSON: [{name, status, port}]
    jarvis_processes TEXT,                      -- JARVIS-related PIDs
    claude_sessions  TEXT,                      -- Active Claude Code sessions
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Periodic snapshots only when JARVIS is running
-- No continuous surveillance
-- No screen content, no keystrokes, no clipboard
```

---

## 5. Technology Stack (Finalized)

| Layer | Technology | Version | Status | Notes |
|---|---|---|---|---|
| Language | Python | 3.12.1 | ✅ Installed | |
| API framework | FastAPI | 0.138 | ✅ In venv | |
| ASGI server | uvicorn | — | ✅ In venv | |
| Database | SQLite | stdlib | ✅ Available | WAL mode |
| Async DB | aiosqlite | latest | ❌ Install | Phase 1 |
| ORM | SQLAlchemy 2.x | latest | ❌ Install | Phase 1 |
| Config | python-dotenv | — | ✅ In venv | |
| Scheduling | APScheduler | 3.11.2 | ✅ In venv | |
| Testing | pytest | — | ✅ In venv | |
| Secrets | cryptography | latest | ❌ Install | Phase 1 |
| Validation | Pydantic | 2.13 | ✅ In venv | |
| Desktop (Windows) | pywin32 | latest | ❌ Install | Phase 8 |
| Desktop GUI fallback | uiautomation | latest | ❌ Install | Phase 8 |
| Process management | psutil | latest | ❌ Install | Phase 8 |
| Voice (future) | OpenAI Realtime API | — | ❌ Credential | Phase 10 |
| iOS (future) | Swift / SwiftUI | — | New | Phase 11 |
| Telephony (future) | Twilio / similar | — | ❌ Account | Phase 12 |

---

## 6. Dependencies, Credentials & External Services

### 6.1 Credentials needed (none for Phase 1)

| Credential | Purpose | Needed For | Status |
|---|---|---|---|
| OpenAI API key | Voice interface | Phase 10 | ❌ Not needed yet |
| Gmail OAuth2 | Gmail integration | Phase 9 | ❌ Not needed yet |
| Google Calendar API | Calendar | Phase 9 | ❌ Not needed yet |
| GitHub PAT | GitHub integration | Phase 9 | ❌ Not needed yet |
| Twilio / similar | Telephony | Phase 12 | ❌ Not needed yet |
| iOS Developer Account | iPhone app | Phase 11 | ❌ Not needed yet |
| Claude Code CLI | Agent worker | Phase 5 | ✅ Installed |

### 6.2 Phase 1 dependencies only

| Package | Install command | Purpose |
|---|---|---|
| aiosqlite | `pip install aiosqlite` | Async SQLite access |
| sqlalchemy | `pip install sqlalchemy` | ORM + migrations |
| cryptography | `pip install cryptography` | Secret vault (AES-256-GCM) |
| pywin32 | `pip install pywin32` | Desktop control (Phase 8, but install early) |
| psutil | `pip install psutil` | Process management (Phase 8) |

---

## 7. Risk Register

| Risk | Severity | Likelihood | Mitigation | Owner |
|---|---|---|---|---|
| **Local PC clock is GMT+10, not IST** | High | Certain | All timestamps UTC. IST only at display boundary. Use `datetime.now(timezone.utc)` everywhere. | Phase 1 |
| **Local PC sleeps/reboots → JARVIS offline** | High | Certain | Auto-start via Windows Task Scheduler at boot. WAL-mode SQLite survives unclean shutdown. User knows limitation (local-first decision). | Phase 1 |
| **Port conflict with dashboard (8000)** | Medium | Low | JARVIS on 8001. Documented. | Phase 1 |
| **Scope creep** | High | High | Phase 1 = foundation only. No features. No UI. Spec mandates this. | Phase 1 |
| **Secret management on local disk** | Medium | Medium | AES-256-GCM vault. Never plaintext. | Phase 7 |
| **Single SQLite writer bottleneck** | Low | Low | WAL mode. One writer at a time. Acceptable for Phase 1. Upgrade to Postgres only if needed. | Phase 1 |
| **Desktop control privilege escalation** | High | Medium | Permission levels (L0–L4). Human override always wins. Shell whitelist. Audit trail. | Phase 7–8 |
| **GUI automation fragility** | Medium | Medium | Prefer APIs/CLIs. GUI automation is fallback only. Verify post-conditions. | Phase 8 |
| **Claude Code session discovery breaks** | Medium | Low | Build on proven `rebuild_desktop_index.py` logic. Fall back to manual project path. | Phase 5 |
| **iPhone app complexity underestimated** | Medium | Medium | Phase 11 is explicitly separate. REST API in Phase 1 enables it. | Phase 11 |
| **Observability gap** | Medium | Medium | Structured JSON logging from Day 1. Self-diagnostics from Phase 14. | Phase 1 |
| **Backup/restore failure** | Medium | Low | Encrypted backups. Integrity verification. Restore tested. | Phase 14 |

---

## 8. Security Model

### 8.1 Security Boundaries

```
┌─────────────────────────────────────────────────────────────┐
│  Local PC (authorized device)                                │
│  ┌─────────────────────────────────────────────────────────┐│
│  │  JARVIS FastAPI (127.0.0.1:8001) — no external binding ││
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐ ││
│  │  │ Vault     │  │ Audit    │  │ Desktop Control      │ ││
│  │  │ (encrypted│  │ (append- │  │ (permission-gated,   │ ││
│  │  │  secrets) │  │  only)   │  │  verified, audited)  │ ││
│  │  └──────────┘  └──────────┘  └──────────────────────┘ ││
│  └─────────────────────────────────────────────────────────┘│
│         │                    │                    │          │
│    ┌────▼────┐         ┌────▼────┐         ┌────▼────┐     │
│    │ Claude  │         │ Trading │         │ Obsidian│     │
│    │ Code    │         │ Systems │         │ Vault   │     │
│    └─────────┘         └─────────┘         └─────────┘     │
└─────────────────────────────────────────────────────────────┘
        ▲
        │ (future: encrypted tunnel)
        │
   ┌────┴──────────────────────────┐
   │  iPhone App (Phase 11)         │
   │  Authenticated + authorized    │
   └────────────────────────────────┘
```

### 8.2 Security Principles

1. **Never fabricate** — JARVIS retrieves and verifies from real sources
2. **Verify important information** — post-conditions, source checks, hash verification
3. **Inspect before modifying** — read source, understand context, check git state
4. **Preserve immutable history** — command ledger and audit trail are append-only
5. **Never silently delete** — soft deletes only, with audit trail
6. **Respect permissions** — L0–L4 model, human override always wins
7. **Ask before high-risk** — L2+ requires confirmation unless standing permission
8. **Explain uncertainty** — distinguish verified/current/historical/inferred
9. **Maintain source provenance** — every fact links to its source
10. **Prefer reversible actions** — sandbox → verify → approve → production
11. **Test before deployment** — no untested code in production paths
12. **Escalate when blocked** — bounded retries, then report to user
13. **Learn from outcomes** — self-improvement loop (Phase 15)
14. **Never confuse memory with source** — JARVIS memory indexes, doesn't replace
15. **Explicit override wins** — user's current instruction > learned preference

---

## 9. Phased Implementation Plan (Updated with Desktop Control)

### Phase 1 — Foundation (4–6 weeks)

**Deliverables:**
- `jarvis/` directory structure (all base files)
- `config.py` — central config, UTC timestamp enforcement, env loading
- `constants.py` — all enums (permissions L0–L4, statuses, agent IDs)
- `exceptions.py` — JARVIS exception hierarchy
- `logging_config.py` — structured JSON logging
- `main.py` — FastAPI app, lifespan, `/api/health`, WebSocket stub
- `memory/database.py` — SQLite init (WAL), connection pool, migration runner
- `memory/command_ledger.py` — immutable command history table + insert
- `memory/goals.py` — goals table + CRUD
- `memory/tasks.py` — tasks table + CRUD + dependency graph
- `memory/decisions.py` — decisions table + CRUD
- `memory/timeline.py` — unified chronological event stream
- `security/audit.py` — append-only audit trail (hash-chained)
- `security/permissions.py` — permission levels + check decorator
- `security/kill_switch.py` — emergency stop
- `desktop/app_registry.py` — application registry table + seed data
- `desktop/project_map.py` — project-to-application mapping
- `CONSTITUTION.md` — JARVIS Constitution (15 principles)
- `docs/ARCHITECTURE.md` — this document
- `docs/API.md` — initial API reference
- `docs/DEPLOYMENT.md` — local PC deployment runbook
- `docs/RUNBOOK.md` — operations runbook
- `tests/` — 200+ tests covering all Phase 1 modules
- `scripts/init_db.py` — one-time DB creation (idempotent)
- `scripts/seed_apps.py` — seed default application registry
- `requirements.txt` — JARVIS dependencies
- `.env.example` — template
- Windows Task Scheduler task for JARVIS auto-start at boot

**Acceptance criteria:**
- `python -m jarvis` starts FastAPI on port 8001
- `GET /api/health` returns 200 with component status
- `POST /api/command` records command in immutable ledger
- SQLite DB created with WAL mode, all tables present
- All 200+ tests pass: `pytest jarvis/tests/ -v --tb=short`
- `python scripts/init_db.py` is idempotent
- `python scripts/seed_apps.py` populates app registry with known apps
- JARVIS auto-starts on Windows boot
- CONSTITUTION.md exists and has no UPDATE/DELETE path in code
- No modifications to existing trading/deployment code

### Phase 2 — JARVIS Core (3–4 weeks)

Intent engine, context manager, conversation manager, tool registry, agent registry.

### Phase 3 — Memory (3–4 weeks)

Full memory hierarchy (16 domains), command ledger, project memory, timeline, decision memory, memory correction.

### Phase 4 — Goals/Tasks (3–4 weeks)

Goal system, task decomposition, dependencies, priorities, commitments, project health, "what am I forgetting?"

### Phase 5 — Claude Code (3–4 weeks)

Project registry, session registry, natural session search, delegation, monitoring, verification.

### Phase 6 — Agent Runtime (3–4 weeks)

Agent lifecycle, permissions, missions, task execution, handoffs, accountability, retries, stop conditions.

### Phase 7 — Security (3–4 weeks)

Authentication, authorization, encrypted vault, permission levels (including expiry), kill switch, audit trail, sandboxing, preflight.

### Phase 8 — Knowledge + Desktop Control (4–5 weeks)

Obsidian indexing, GitHub integration, project indexing, knowledge graph, cross-source search,
**AND Desktop Control Layer: app registry, project mapping, launcher, shell, GUI, state awareness,
verification, multi-device support, desktop audit trail.**

### Phase 9 — External Systems (3–4 weeks)

Gmail, Calendar, Google Sheets, trading APIs, website APIs, files, remote desktop support.

### Phase 10 — Voice (3–4 weeks)

OpenAI Realtime Voice, speech I/O, tool invocation, interruptions, driving/desk modes, **voice desktop control.**

### Phase 11 — Mobile (4–6 weeks)

iPhone app: Siri/App Intents, notifications, Live Activities, CarPlay, approval UI, voice interface.

### Phase 12 — Telephony (3–4 weeks)

Outbound calls, two-way voice, call state, authentication.

### Phase 13 — Proactive JARVIS (3–4 weeks)

Deadline monitoring, goal monitoring, attention management, briefings, predictive assistance.

### Phase 14 — Reliability (3–4 weeks)

Self-diagnostics, backup/restore, memory integrity, rollback, evidence bundles, **desktop self-diagnostics.**

### Phase 15 — Advanced Intelligence (4–6 weeks)

Self-improvement, pattern detection, anomaly detection, automation suggestions, contextual memory compression, confidence system, self-reflection, VPS migration path.

---

## 10. Phase 1 Detailed Specification

### 10.1 Files to create (20 files)

| # | File | Purpose |
|---|---|---|
| 1 | `jarvis/__init__.py` | Package init, version |
| 2 | `jarvis/main.py` | FastAPI app, lifespan, health endpoint |
| 3 | `jarvis/config.py` | Central config, env loading, UTC enforcement |
| 4 | `jarvis/constants.py` | Permission levels, status enums, agent IDs |
| 5 | `jarvis/exceptions.py` | Exception hierarchy |
| 6 | `jarvis/logging_config.py` | Structured JSON logging |
| 7 | `jarvis/CONSTITUTION.md` | 15 immutable principles |
| 8 | `jarvis/memory/database.py` | SQLite init + WAL + connection management |
| 9 | `jarvis/memory/command_ledger.py` | Immutable command history |
| 10 | `jarvis/memory/goals.py` | Goals CRUD |
| 11 | `jarvis/memory/tasks.py` | Tasks CRUD + dependency graph |
| 12 | `jarvis/memory/decisions.py` | Decisions CRUD |
| 13 | `jarvis/memory/timeline.py` | Unified event stream |
| 14 | `jarvis/security/audit.py` | Append-only audit trail |
| 15 | `jarvis/security/permissions.py` | Permission checks |
| 16 | `jarvis/security/kill_switch.py` | Emergency stop |
| 17 | `jarvis/desktop/app_registry.py` | App registry table + seed |
| 18 | `jarvis/desktop/project_map.py` | Project-to-app mapping |
| 19 | `jarvis/tests/conftest.py` | Test fixtures |
| 20 | `jarvis/tests/__init__.py` | Test package |

### 10.2 Database tables created in Phase 1

| Table | Purpose | Phase |
|---|---|---|
| `command_ledger` | Immutable command history | 1 |
| `goals` | Goals + milestones + deadlines | 1 |
| `tasks` | Tasks + dependencies + priorities | 1 |
| `decisions` | Decision memory | 1 |
| `timeline` | Unified event stream | 1 |
| `audit_trail` | Append-only audit (hash-chained) | 1 |
| `app_registry` | Application registry (desktop) | 1 |
| `project_map` | Project-to-application mapping | 1 |

### 10.3 API endpoints in Phase 1

| Method | Path | Purpose | Auth |
|---|---|---|---|
| GET | `/api/health` | System health check | None |
| GET | `/api/health/diagnose` | Self-diagnostics | None |
| POST | `/api/command` | Submit command (records to ledger) | None (Phase 1) |
| GET | `/api/memory/commands` | Query command history | None (Phase 1) |
| GET | `/api/memory/timeline` | Get timeline events | None (Phase 1) |
| GET | `/api/goals` | List goals | None (Phase 1) |
| POST | `/api/goals` | Create goal | None (Phase 1) |
| GET | `/api/tasks` | List tasks | None (Phase 1) |
| POST | `/api/tasks` | Create task | None (Phase 1) |
| GET | `/api/projects` | List registered projects | None (Phase 1) |
| WS | `/ws` | WebSocket for real-time push | None (Phase 1) |

### 10.4 Tests in Phase 1 (target: 200+)

| Test file | Tests | Coverage |
|---|---|---|
| `test_database.py` | 15 | DB init, WAL mode, migrations, connection, idempotency |
| `test_command_ledger.py` | 20 | Immutable insert, retrieval, timestamp UTC, no UPDATE/DELETE |
| `test_goals.py` | 25 | CRUD, milestones, deadlines, success criteria |
| `test_tasks.py` | 25 | CRUD, dependencies, priorities, states, cycles |
| `test_decisions.py` | 15 | CRUD, evidence, reasoning |
| `test_timeline.py` | 20 | Chronological ordering, cross-source events, filtering |
| `test_audit.py` | 20 | Append-only, hash-chain, no tampering, retrieval |
| `test_permissions.py` | 20 | L0–L4 checks, human override, permission expiry |
| `test_kill_switch.py` | 10 | Activation, state, recovery |
| `test_config.py` | 10 | Env loading, UTC enforcement, defaults |
| `test_constants.py` | 5 | Enum values, immutability |
| `test_logging.py` | 10 | JSON format, file output, structured fields |
| `test_main.py` | 10 | App startup, health endpoint, lifespan |
| `test_app_registry.py` | 15 | CRUD, seed data, aliases, OS filtering |
| `test_project_map.py` | 10 | CRUD, project-app relationships |
| `test_api_commands.py` | 10 | POST /api/command, validation, recording |
| `test_api_health.py` | 5 | GET /api/health, diagnose |
| `test_api_goals.py` | 10 | CRUD endpoints |
| `test_api_tasks.py` | 10 | CRUD endpoints |
| `test_integrations_fs.py` | 10 | File operations (read-only) |
| `test_constitution.py` | 5 | CONSTITUTION.md exists, immutable |
| **Total** | **250** | |

---

## 11. Phase 1 — No-Modification Guarantee

**JARVIS Phase 1 will NOT modify any of the following:**

| Directory/File | Reason |
|---|---|
| `deployment/` | Live trading dashboard |
| `live_trading_options/` | Live options engines (Vwap, DN, BTC) |
| `backtesting/` | Backtesting engines |
| `options/` | Options pipeline |
| `data/` | Trading data |
| `config/` | Trading config |
| `auth/` | Fyers auth |
| `downloader/` | Data downloader |
| `tracker/` | Data manifest |
| `logs/` | Trading logs |
| `run_pipeline.py` | Trading pipeline runner |
| `daily_update.bat` | Trading data update |
| `morning_login.bat` / `fetch_fyers_token_VPS.bat` | Token management |
| `CLAUDE.md` | Project instructions (read-only reference) |
| `G:\Trading Brain\` | Obsidian vault (read-only reference) |

JARVIS lives entirely within `G:\fyers_data_pipeline\jarvis\`.

---

## 12. Phase 1 — Acceptance Criteria (Final Checklist)

Before Phase 1 is considered complete, ALL of the following must be true:

- [ ] `jarvis/` directory exists with all 20+ source files
- [ ] `python -m jarvis` starts without errors on port 8001
- [ ] `GET /api/health` returns HTTP 200 with JSON status for each component
- [ ] `POST /api/command` with `{"command": "test"}` returns a command_id and records the command
- [ ] SQLite DB file exists at `jarvis/data/jarvis.db` in WAL mode
- [ ] `jarvis/data/jarvis.db` has all 8 tables with correct schemas
- [ ] `python scripts/init_db.py` runs without errors and is idempotent (run twice, no errors)
- [ ] `python scripts/seed_apps.py` populates app_registry with known Windows apps
- [ ] All 200+ tests pass: `pytest jarvis/tests/ -v --tb=short`
- [ ] `CONSTITUTION.md` exists with 15 principles and has no UPDATE/DELETE code path
- [ ] JARVIS auto-starts on Windows boot (Task Scheduler task created)
- [ ] No existing trading/deployment code was modified
- [ ] `requirements.txt` includes all JARVIS-specific dependencies
- [ ] `.env.example` exists with all required variables documented
- [ ] Structured JSON logs are written to `jarvis/logs/`
- [ ] Command ledger rejects UPDATE/DELETE operations (tested)
- [ ] Audit trail is append-only with hash-chain verification (tested)
- [ ] Permission checks block unauthorized actions (tested)
- [ ] All timestamps in DB are UTC (tested)
- [ ] Kill switch stops all active operations (tested)

---

## 13. What Phase 1 Does NOT Include

Explicitly out of scope for Phase 1:

- ❌ No voice interface (Phase 10)
- ❌ No iPhone app (Phase 11)
- ❌ No telephony (Phase 12)
- ❌ No Gmail/Calendar/Sheets (Phase 9)
- ❌ No Obsidian indexing (Phase 8)
- ❌ No GitHub integration (Phase 9)
- ❌ No Claude Code delegation (Phase 5)
- ❌ No desktop control execution (Phase 8 — registry + mapping only)
- ❌ No goal/task completion workflows (Phase 4)
- ❌ No proactive intelligence (Phase 13)
- ❌ No self-diagnostics (Phase 14)
- ❌ No trading system integration (Phase 9)
- ❌ No UI beyond basic API docs
- ❌ No user-facing features
- ❌ No modifications to existing code

Phase 1 is bones, tests, and documentation. Nothing else.

---

*End of Phase 0 Architecture Document*
*Next: User approval → Phase 1 implementation*
