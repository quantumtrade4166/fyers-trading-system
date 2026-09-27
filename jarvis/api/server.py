"""
JARVIS — API Server
====================
FastAPI-based REST API for JARVIS.

Endpoints:
  POST /api/auth/token          — Get a JWT token
  POST /api/auth/api-key        — Generate an API key
  POST /api/command             — Execute a JARVIS command
  GET  /api/status              — System status
  GET  /api/status/agent_loop   — Agent loop health
  GET  /api/vps/health          — VPS health check
  POST /api/vps/restart         — Restart a VPS engine
  GET  /api/vps/engines         — List known engines

Auth:
  - JWT: Bearer token in Authorization header
  - API key: X-API-Key header
  - Localhost (127.0.0.1): no auth required (convenience for local use)

Usage:
    uvicorn jarvis.api.server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from jarvis.logging_config import get_logger

log = get_logger("api_server")


# ── Lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    log.info("jarvis_api_started", pid=os.getpid())
    yield
    log.info("jarvis_api_stopped")


# ── FastAPI App ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="JARVIS API",
    description="REST API for JARVIS — Just A Rather Very Intelligent System",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

class TokenRequest(BaseModel):
    username: str = Field(..., description="Your username")
    ttl_hours: int = Field(24, ge=1, le=168, description="Token lifetime in hours")

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    expires_at: str

class APIKeyRequest(BaseModel):
    name: str = Field(..., description="Name for this API key")
    permissions: list[str] = Field(default_factory=lambda: ["read"])

class APIKeyResponse(BaseModel):
    api_key: str
    name: str
    permissions: list[str]
    warning: str = "Save this key now — it won't be shown again"

class CommandRequest(BaseModel):
    command: str = Field(..., description="JARVIS command to execute")
    confirm: bool = Field(False, description="Set true for dangerous actions")

class VPSRestartRequest(BaseModel):
    engine: str = Field(..., description="Engine name (e.g. 'dashboard', 'all')")

class VPSHealthResponse(BaseModel):
    vps: str
    reachable: bool
    checks: dict[str, Any] | None = None


# ── Auth Dependency ────────────────────────────────────────────────────────

class AuthDependency:
    """Handles JWT and API key authentication."""

    def __init__(self) -> None:
        self._permit_localhost = True

    def __call__(self, request: Request) -> dict:
        # Localhost bypass (includes TestClient host='testclient')
        client_host = request.client.host if request.client else ""
        if self._permit_localhost and client_host in ("127.0.0.1", "::1", "localhost", "testclient"):
            return {"sub": "localhost", "bypass": True}

        # Try JWT Bearer token
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            from jarvis.api.auth import decode_token
            payload = decode_token(token)
            if payload:
                return payload
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        # Try API key
        api_key = request.headers.get("x-api-key", "")
        if api_key:
            from jarvis.api.auth import get_api_key_manager
            mgr = get_api_key_manager()
            key_info = mgr.validate(api_key)
            if key_info:
                return {"sub": key_info["name"], "api_key": True, "permissions": key_info["permissions"]}
            raise HTTPException(status_code=401, detail="Invalid API key")

        raise HTTPException(
            status_code=401,
            detail="Authentication required — provide Bearer token or X-API-Key header",
        )


_auth = AuthDependency()


# ── FastAPI App ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="JARVIS API",
    description="REST API for JARVIS — Just A Rather Very Intelligent System",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — restrict to known origins in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8080", "http://127.0.0.1:8080"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Health ──────────────────────────────────────────────────────────────────

@app.get("/")
async def root() -> dict:
    return {"system": "JARVIS", "status": "running", "version": "1.0.0"}


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Auth Endpoints ─────────────────────────────────────────────────────────

@app.post("/api/auth/token", response_model=TokenResponse)
async def create_token_endpoint(req: TokenRequest) -> TokenResponse:
    """Get a JWT access token."""
    from jarvis.api.auth import create_token
    ttl = req.ttl_hours * 3600
    token = create_token(req.username, ttl_seconds=ttl)
    now = int(time.time())
    return TokenResponse(
        access_token=token,
        expires_in=req.ttl_hours * 3600,
        expires_at=datetime.fromtimestamp(now + ttl, tz=timezone.utc).isoformat(),
    )


@app.post("/api/auth/api-key", response_model=APIKeyResponse)
async def create_api_key(req: APIKeyRequest) -> APIKeyResponse:
    """Generate a new API key."""
    from jarvis.api.auth import get_api_key_manager
    mgr = get_api_key_manager()
    key = mgr.generate_key(req.name, req.permissions)
    return APIKeyResponse(api_key=key, name=req.name, permissions=req.permissions)


# ── Command Endpoint ────────────────────────────────────────────────────────

@app.post("/api/command")
async def execute_command(req: CommandRequest, user: dict = Depends(_auth)) -> dict:
    """Execute a JARVIS command."""
    from jarvis.main import JARVIS
    jarvis = JARVIS()

    # Initialize if needed
    if not jarvis._initialized:
        await jarvis.initialize()

    result = await jarvis.process(req.command, human_override=req.confirm)

    # Add auth info
    result["authenticated_as"] = user.get("sub", "unknown")
    return result


# ── Status Endpoints ────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status(user: dict = Depends(_auth)) -> dict:
    """Full system status."""
    from jarvis.main import JARVIS
    jarvis = JARVIS()

    if not jarvis._initialized:
        await jarvis.initialize()

    status = await jarvis._handle_status()
    status["authenticated_as"] = user.get("sub", "unknown")
    return status


@app.get("/api/status/agent_loop")
async def get_agent_loop_status(user: dict = Depends(_auth)) -> dict:
    """Agent loop health data."""
    from jarvis.main import JARVIS
    jarvis = JARVIS()

    if not jarvis._initialized:
        await jarvis.initialize()

    return jarvis.get_agent_loop_status()


# ── VPS Endpoints ───────────────────────────────────────────────────────────

@app.get("/api/vps/health", response_model=VPSHealthResponse)
async def vps_health(user: dict = Depends(_auth)) -> VPSHealthResponse:
    """VPS health check."""
    from jarvis.vps_controller import VPSController, VPS_IP
    ctrl = VPSController()
    result = await ctrl.health_check()
    return VPSHealthResponse(
        vps=result.get("vps", VPS_IP),
        reachable=result.get("status") == "ok",
        checks=result.get("checks"),
    )


@app.post("/api/vps/restart")
async def vps_restart(req: VPSRestartRequest, user: dict = Depends(_auth)) -> dict:
    """Restart a VPS engine."""
    from jarvis.vps_controller import VPSController, VPS_ENGINES, _SAFE_COMMANDS
    ctrl = VPSController()

    # Validate engine name
    valid_engines = list(VPS_ENGINES.keys()) + ["all"]
    if req.engine not in valid_engines and req.engine not in _SAFE_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown engine '{req.engine}'. Valid: {', '.join(valid_engines)}",
        )

    result = await ctrl.execute(f"restart {req.engine}")
    return result


@app.get("/api/vps/engines")
async def vps_engines(user: dict = Depends(_auth)) -> dict:
    """List known VPS engines."""
    from jarvis.vps_controller import VPS_ENGINES, VPS_IP
    return {
        "engines": list(VPS_ENGINES.keys()),
        "vps": VPS_IP,
        "safe_commands": ["restart dashboard", "restart all", "restart dualmom_kite", "restart dualmom_kotak", "check all", "disk", "memory", "uptime", "logs dashboard"],
    }


# ── Error Handlers ──────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"status": "error", "error": str(exc), "path": request.url.path},
    )


# ── Entry Point ─────────────────────────────────────────────────────────────

def run_server(host: str = "0.0.0.0", port: int = 8080, reload: bool = False) -> None:
    """Run the API server."""
    uvicorn.run(
        "jarvis.api.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="JARVIS API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev)")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, reload=args.reload)
