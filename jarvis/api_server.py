"""
JARVIS — FastAPI Server
========================
REST API for the JARVIS system.

Endpoints:
  POST /api/command      — Process a natural language command
  GET  /api/status       — System status
  GET  /api/goals        — List goals
  POST /api/goals        — Create a goal
  GET  /api/goals/{id}   — Get a goal
  PATCH /api/goals/{id}  — Update a goal
  GET  /api/tasks        — List tasks
  POST /api/tasks        — Create a task
  GET  /api/tasks/{id}   — Get a task
  PATCH /api/tasks/{id}  — Update a task
  POST /api/tasks/{id}/dependencies — Add dependency
  GET  /api/decisions    — List decisions
  POST /api/decisions    — Record a decision
  GET  /api/timeline     — Timeline events
  GET  /api/apps         — List registered apps
  POST /api/apps         — Register an app
  POST /api/kill-switch  — Activate kill switch
  GET  /api/kill-switch  — Check kill switch status
  GET  /api/audit        — Audit trail
  POST /api/desktop      — Execute desktop action

Rules:
  - CORS enabled for localhost only (no external origins).
  - All endpoints require the same safety checks as CLI (kill switch + permissions).
  - No secrets exposed in any response.
  - Phase 1: no auth (localhost only). Phase 2+: JWT.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

# Encoding fix
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Ensure project root in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from jarvis.memory.database import Database, get_db
from jarvis.memory.goals import GoalsManager, new_goal_id
from jarvis.memory.tasks import TasksManager, new_task_id
from jarvis.memory.decisions import Decisions, new_decision_id
from jarvis.memory.timeline import Timeline, new_event_id
from jarvis.desktop.remote_control import DesktopController
from jarvis.security.audit import AuditTrail, utc_now
from jarvis.security.kill_switch import get_kill_switch
from jarvis.logging_config import get_logger
from jarvis.config import get_config

log = get_logger("api")

app = FastAPI(
    title="JARVIS API",
    description="REST API for JARVIS — Just A Rather Very Intelligent System",
    version="0.1.0",
)

# CORS: localhost only
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:*", "http://127.0.0.1:*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

config = get_config()


# ── Request Models ────────────────────────────────────────────────────────
class CommandRequest(BaseModel):
    command: str
    human_override: bool = False


class GoalCreateRequest(BaseModel):
    title: str
    description: str = ""
    priority: int = 5
    deadline: Optional[str] = None
    rationale: str = ""


class TaskCreateRequest(BaseModel):
    title: str
    description: str = ""
    priority: int = 5
    goal_id: Optional[str] = None
    project: Optional[str] = None
    deadline: Optional[str] = None


class DecisionCreateRequest(BaseModel):
    title: str
    decision: str
    reasoning: str = ""
    alternatives: str = ""
    evidence: str = ""
    context: str = ""
    project: Optional[str] = None


class DesktopActionRequest(BaseModel):
    action: str
    target: str
    working_dir: str = ""
    args: str = ""
    human_override: bool = False


class DependencyRequest(BaseModel):
    depends_on: str


# ── Startup ───────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup() -> None:
    db = Database()
    await db.initialize()
    controller = DesktopController(db)
    await controller.initialize()


# ── Error Handler ─────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "code": "INTERNAL_ERROR"},
    )


# ── Health ────────────────────────────────────────────────────────────────
@app.get("/")
async def root() -> dict:
    return {"name": "JARVIS", "version": "0.1.0", "status": "running"}


@app.get("/health")
async def health() -> dict:
    try:
        db = await get_db()
        tables = await db.list_tables()
        return {
            "status": "healthy",
            "database": "connected",
            "tables": tables,
            "kill_switch": get_kill_switch().is_active,
            "timestamp": utc_now(),
        }
    except Exception as e:
        return {"status": "unhealthy", "error": str(e), "timestamp": utc_now()}


# ── Command Endpoint ──────────────────────────────────────────────────────
@app.post("/api/command")
async def process_command(req: CommandRequest) -> dict:
    """Process a natural language command."""
    from jarvis.main import JARVIS
    jarvis = JARVIS()
    result = await jarvis.process(req.command, human_override=req.human_override)
    status_code = 200 if result.get("status") != "error" else 400
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail=result)
    return result


# ── Goals ─────────────────────────────────────────────────────────────────
@app.get("/api/goals")
async def list_goals(status: Optional[str] = None, project: Optional[str] = None) -> dict:
    goals_mgr = GoalsManager()
    goals = await goals_mgr.goals.get_all(status=status, project=project)
    return {"status": "ok", "count": len(goals), "goals": goals}


@app.post("/api/goals")
async def create_goal(req: GoalCreateRequest) -> dict:
    goals_mgr = GoalsManager()
    goal = await goals_mgr.goals.create(
        title=req.title,
        description=req.description,
        priority=req.priority,
        deadline=req.deadline,
        rationale=req.rationale,
    )
    return {"status": "ok", "goal": goal}


@app.get("/api/goals/{goal_id}")
async def get_goal(goal_id: str) -> dict:
    goals_mgr = GoalsManager()
    goal = await goals_mgr.goals.get(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail={"error": "Goal not found", "goal_id": goal_id})
    return {"status": "ok", "goal": goal}


@app.patch("/api/goals/{goal_id}")
async def update_goal(goal_id: str, updates: dict) -> dict:
    goals_mgr = GoalsManager()
    # Validate immutable fields
    forbidden = {"goal_id", "created_at"}
    for f in updates:
        if f in forbidden:
            raise HTTPException(status_code=403, detail={"error": f"Cannot update '{f}'"})
    goal = await goals_mgr.goals.update(goal_id, **updates)
    if not goal:
        raise HTTPException(status_code=404, detail={"error": "Goal not found"})
    return {"status": "ok", "goal": goal}


@app.patch("/api/goals/{goal_id}/complete")
async def complete_goal(goal_id: str) -> dict:
    goals_mgr = GoalsManager()
    goal = await goals_mgr.goals.complete(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail={"error": "Goal not found"})
    return {"status": "ok", "goal": goal}


# ── Milestones ────────────────────────────────────────────────────────────
@app.post("/api/goals/{goal_id}/milestones")
async def create_milestone(goal_id: str, title: str, description: str = "",
                           due_date: Optional[str] = None) -> dict:
    goals_mgr = GoalsManager()
    ms = await goals_mgr.milestones.create(goal_id, title, description, due_date)
    return {"status": "ok", "milestone": ms}


@app.get("/api/goals/{goal_id}/milestones")
async def list_milestones(goal_id: str) -> dict:
    goals_mgr = GoalsManager()
    milestones = await goals_mgr.milestones.get_for_goal(goal_id)
    return {"status": "ok", "count": len(milestones), "milestones": milestones}


# ── Tasks ─────────────────────────────────────────────────────────────────
@app.get("/api/tasks")
async def list_tasks(
    state: Optional[str] = None,
    project: Optional[str] = None,
    goal_id: Optional[str] = None,
) -> dict:
    tasks_mgr = TasksManager()
    tasks = await tasks_mgr.tasks.get_all(state=state, project=project, goal_id=goal_id)
    return {"status": "ok", "count": len(tasks), "tasks": tasks}


@app.post("/api/tasks")
async def create_task(req: TaskCreateRequest) -> dict:
    tasks_mgr = TasksManager()
    task = await tasks_mgr.tasks.create(
        title=req.title,
        description=req.description,
        priority=req.priority,
        goal_id=req.goal_id,
        project=req.project,
        deadline=req.deadline,
    )
    return {"status": "ok", "task": task}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    tasks_mgr = TasksManager()
    task = await tasks_mgr.tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail={"error": "Task not found"})
    return {"status": "ok", "task": task}


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, updates: dict) -> dict:
    tasks_mgr = TasksManager()
    forbidden = {"task_id", "created_at"}
    for f in updates:
        if f in forbidden:
            raise HTTPException(status_code=403, detail={"error": f"Cannot update '{f}'"})
    task = await tasks_mgr.tasks.update(task_id, **updates)
    if not task:
        raise HTTPException(status_code=404, detail={"error": "Task not found"})
    return {"status": "ok", "task": task}


@app.patch("/api/tasks/{task_id}/state")
async def set_task_state(task_id: str, state: str) -> dict:
    tasks_mgr = TasksManager()
    task = await tasks_mgr.tasks.set_state(task_id, state)
    if not task:
        raise HTTPException(status_code=404, detail={"error": "Task not found"})
    return {"status": "ok", "task": task}


# ── Task Dependencies ─────────────────────────────────────────────────────
@app.post("/api/tasks/{task_id}/dependencies")
async def add_dependency(task_id: str, req: DependencyRequest) -> dict:
    tasks_mgr = TasksManager()
    added = await tasks_mgr.tasks.add_dependency(task_id, req.depends_on)
    if not added:
        return {"status": "ok", "message": "Dependency already exists or would create a cycle"}
    return {"status": "ok", "task_id": task_id, "depends_on": req.depends_on}


@app.get("/api/tasks/{task_id}/dependencies")
async def get_dependencies(task_id: str) -> dict:
    tasks_mgr = TasksManager()
    deps = await tasks_mgr.tasks.get_dependencies(task_id)
    return {"status": "ok", "task_id": task_id, "dependencies": deps}


@app.get("/api/tasks/{task_id}/dependents")
async def get_dependents(task_id: str) -> dict:
    tasks_mgr = TasksManager()
    dependents = await tasks_mgr.tasks.get_dependents(task_id)
    return {"status": "ok", "task_id": task_id, "dependents": dependents}


# ── Decisions ─────────────────────────────────────────────────────────────
@app.get("/api/decisions")
async def list_decisions(project: Optional[str] = None) -> dict:
    dec = Decisions()
    if project:
        decisions = await dec.get_for_project(project)
    else:
        decisions = await dec.search("")
    return {"status": "ok", "count": len(decisions), "decisions": decisions}


@app.post("/api/decisions")
async def create_decision(req: DecisionCreateRequest) -> dict:
    dec = Decisions()
    decision = await dec.create(
        title=req.title,
        decision=req.decision,
        reasoning=req.reasoning,
        alternatives=req.alternatives,
        evidence=req.evidence,
        context=req.context,
        project=req.project,
    )
    return {"status": "ok", "decision": decision}


# ── Timeline ──────────────────────────────────────────────────────────────
@app.get("/api/timeline")
async def get_timeline(
    project: Optional[str] = None,
    event_type: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 100,
) -> dict:
    timeline = Timeline()
    events = await timeline.get(
        project=project, event_type=event_type, start=start, end=end, limit=limit
    )
    return {"status": "ok", "count": len(events), "events": events}


# ── Desktop ───────────────────────────────────────────────────────────────
@app.post("/api/desktop")
async def desktop_action(req: DesktopActionRequest) -> dict:
    controller = DesktopController()
    result = await controller.execute(
        req.action,
        req.target,
        human_override=req.human_override,
    )
    return result


@app.get("/api/apps")
async def list_apps(os_name: str = "windows") -> dict:
    registry = DesktopController()._apps
    apps = await registry.list_all(os_name)
    return {"status": "ok", "count": len(apps), "apps": apps}


# ── Kill Switch ───────────────────────────────────────────────────────────
@app.post("/api/kill-switch")
async def activate_kill_switch() -> dict:
    ks = get_kill_switch()
    ks.activate()
    return {"status": "activated", "message": "Kill switch activated"}


@app.delete("/api/kill-switch")
async def deactivate_kill_switch() -> dict:
    ks = get_kill_switch()
    ks.deactivate()
    return {"status": "deactivated", "message": "Kill switch deactivated"}


@app.get("/api/kill-switch")
async def kill_switch_status() -> dict:
    ks = get_kill_switch()
    return {"active": ks.is_active}


# ── Audit Trail ───────────────────────────────────────────────────────────
@app.get("/api/audit")
async def get_audit(limit: int = 50) -> dict:
    audit = AuditTrail()
    entries = await audit.get_recent(limit=limit)
    verification = await audit.verify_chain()
    return {
        "status": "ok",
        "entries": entries,
        "chain_verification": verification,
    }


# ── App Registry ──────────────────────────────────────────────────────────
@app.post("/api/apps")
async def register_app(name: str, exe_path: str = "", os_name: str = "windows",
                       permission_level: int = 1) -> dict:
    registry = DesktopController()._apps
    result = await registry.register(
        name, exe_path=exe_path, os_name=os_name, permission_level=permission_level
    )
    return {"status": "ok", "app": result}
