"""
JARVIS — API Server
====================
FastAPI REST API for JARVIS.

Endpoints (permission needed in brackets; admin implies all):
  GET    /                         public  — liveness
  GET    /health                   public  — liveness + timestamp
  POST   /api/auth/token           [any]   — mint a JWT (API-key or local callers only;
                                             never more permissions than the caller)
  POST   /api/auth/api-key         [admin] — create an API key (shown once)
  GET    /api/auth/api-keys        [admin] — list key names + permissions
  DELETE /api/auth/api-key/{name}  [admin] — revoke a key
  POST   /api/command              [command]
  GET    /api/status               [read]
  GET    /api/status/agent_loop    [read]
  GET    /api/vps/health           [read]
  GET    /api/vps/engines          [read]
  POST   /api/vps/restart          [vps_restart]

Auth:
  - X-API-Key header, or Authorization: Bearer <JWT>
  - "Direct local" requests (made ON the VPS, loopback address, and carrying
    NO proxy headers) are trusted as admin. Anything relayed by a proxy or a
    Cloudflare tunnel ALWAYS carries such headers, so it must authenticate.
    Set JARVIS_TRUST_LOCAL=0 to disable local trust entirely.

Usage (binds to loopback only — reach it via a tunnel, never the public NIC):
    uvicorn jarvis.api.server:app --host 127.0.0.1 --port 8081
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from jarvis.api.auth import (
    ADMIN, ALL_PERMISSIONS, COMMAND, READ, VPS_RESTART,
    can_grant, create_token, decode_token, get_api_key_manager,
    has_permission, validate_permissions,
)
from jarvis.logging_config import get_logger

log = get_logger("api_server")


# ── Shared JARVIS instance ─────────────────────────────────────────────────
# One JARVIS (one DB connection, ONE agent loop) for the whole process.
# Previously every request built a new JARVIS and started another agent loop.

_jarvis = None
_jarvis_lock: asyncio.Lock | None = None
_jarvis_lock_loop = None


def _get_jarvis_lock() -> asyncio.Lock:
    global _jarvis_lock, _jarvis_lock_loop
    loop = asyncio.get_running_loop()
    if _jarvis_lock is None or _jarvis_lock_loop is not loop:
        _jarvis_lock, _jarvis_lock_loop = asyncio.Lock(), loop
    return _jarvis_lock


async def get_jarvis():
    """Return the process-wide JARVIS, initialising it once."""
    global _jarvis
    if _jarvis is not None:
        return _jarvis
    async with _get_jarvis_lock():
        if _jarvis is None:
            from jarvis.main import JARVIS
            j = JARVIS()
            await j.initialize()
            _jarvis = j
    return _jarvis


# ── Lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("jarvis_api_started", pid=os.getpid())
    yield
    if _jarvis is not None and getattr(_jarvis, "_agent_loop", None) is not None:
        try:
            await _jarvis._agent_loop.stop()
        except Exception as e:  # pragma: no cover - best effort on shutdown
            log.warning("agent_loop_stop_failed", error=str(e))
    log.info("jarvis_api_stopped")


app = FastAPI(
    title="JARVIS API",
    description="REST API for JARVIS — Just A Rather Very Intelligent System",
    version="1.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8080", "http://127.0.0.1:8080"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type"],
)


# ── Models ─────────────────────────────────────────────────────────────────

class TokenRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, description="Who the token is for")
    ttl_hours: int = Field(24, ge=1, le=168, description="Token lifetime in hours")
    permissions: list[str] = Field(default_factory=lambda: [READ],
                                   description=f"Subset of the caller's permissions: {', '.join(ALL_PERMISSIONS)}")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    expires_at: str
    permissions: list[str]


class APIKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="Name for this API key")
    permissions: list[str] = Field(default_factory=lambda: [READ])


class APIKeyResponse(BaseModel):
    api_key: str
    name: str
    permissions: list[str]
    warning: str = "Save this key now — it won't be shown again"


class CommandRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=2000, description="JARVIS command to execute")
    confirm: bool = Field(False, description="Set true for dangerous actions")


class VPSRestartRequest(BaseModel):
    engine: str = Field(..., description="Engine name (e.g. 'dashboard', 'all')")


class VPSHealthResponse(BaseModel):
    vps: str
    reachable: bool
    checks: dict[str, Any] | None = None


# ── Authentication ─────────────────────────────────────────────────────────

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
# Any of these means the request was relayed (Cloudflare tunnel, reverse proxy)
_PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "cdn-loop", "x-forwarded-for",
                  "x-forwarded-host", "x-real-ip", "forwarded")


def is_direct_local(request: Request) -> bool:
    """True only for requests made on this machine WITHOUT going through a proxy."""
    if os.environ.get("JARVIS_TRUST_LOCAL", "1") == "0":
        return False
    host = request.client.host if request.client else ""
    if host not in _LOOPBACK_HOSTS:
        return False
    headers = request.headers
    return not any(h in headers for h in _PROXY_HEADERS)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


class AuthDependency:
    """Resolves the caller to {'sub', 'permissions', 'via'} or raises 401."""

    def __call__(self, request: Request) -> dict:
        if is_direct_local(request):
            return {"sub": "local", "permissions": [ADMIN], "via": "local"}

        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            payload = decode_token(auth_header[7:].strip())
            if not payload:
                raise _unauthorized("Invalid or expired token")
            return {"sub": payload["sub"], "permissions": list(payload.get("permissions", [])), "via": "token"}

        api_key = request.headers.get("x-api-key", "")
        if api_key:
            info = get_api_key_manager().validate(api_key)
            if not info:
                log.warning("api_key_rejected", client=request.client.host if request.client else "")
                raise _unauthorized("Invalid API key")
            return {"sub": info["name"], "permissions": list(info["permissions"]), "via": "api_key"}

        raise _unauthorized("Authentication required — send X-API-Key or Authorization: Bearer <token>")


_auth = AuthDependency()


def require(permission: str) -> Callable[..., dict]:
    """Dependency: authenticate, then insist on `permission` (admin passes all)."""
    def _dep(user: dict = Depends(_auth)) -> dict:
        if not has_permission(user["permissions"], permission):
            raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")
        return user
    return _dep


# ── Public ─────────────────────────────────────────────────────────────────

@app.get("/")
async def root() -> dict:
    return {"system": "JARVIS", "status": "running", "version": app.version}


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Credentials ────────────────────────────────────────────────────────────

@app.post("/api/auth/token", response_model=TokenResponse)
async def create_token_endpoint(req: TokenRequest, user: dict = Depends(_auth)) -> TokenResponse:
    """Mint a JWT. Tokens can't mint tokens (no self-renewal), and never exceed the caller."""
    if user["via"] == "token":
        raise HTTPException(status_code=403, detail="Use an API key to mint tokens")
    try:
        perms = validate_permissions(req.permissions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not can_grant(user["permissions"], perms):
        raise HTTPException(status_code=403, detail="Cannot grant permissions you do not have")
    ttl = req.ttl_hours * 3600
    token = create_token(req.username, ttl_seconds=ttl, permissions=perms, minted_by=user["sub"])
    log.info("token_minted", sub=req.username, perms=perms, minted_by=user["sub"])
    return TokenResponse(
        access_token=token,
        expires_in=ttl,
        expires_at=datetime.fromtimestamp(int(time.time()) + ttl, tz=timezone.utc).isoformat(),
        permissions=perms,
    )


@app.post("/api/auth/api-key", response_model=APIKeyResponse)
async def create_api_key(req: APIKeyRequest, user: dict = Depends(require(ADMIN))) -> APIKeyResponse:
    try:
        key = get_api_key_manager().generate_key(req.name, req.permissions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log.info("api_key_created_via_api", name=req.name, by=user["sub"])
    return APIKeyResponse(api_key=key, name=req.name, permissions=validate_permissions(req.permissions))


@app.get("/api/auth/api-keys")
async def list_api_keys(user: dict = Depends(require(ADMIN))) -> dict:
    return {"keys": get_api_key_manager().list_keys()}


@app.delete("/api/auth/api-key/{name}")
async def revoke_api_key(name: str, user: dict = Depends(require(ADMIN))) -> dict:
    if not get_api_key_manager().revoke(name):
        raise HTTPException(status_code=404, detail=f"No key named '{name}'")
    log.info("api_key_revoked_via_api", name=name, by=user["sub"])
    return {"revoked": name}


# ── Commands & status ──────────────────────────────────────────────────────

@app.post("/api/command")
async def execute_command(req: CommandRequest, user: dict = Depends(require(COMMAND))) -> dict:
    jarvis = await get_jarvis()
    result = await jarvis.process(req.command, human_override=req.confirm)
    result["authenticated_as"] = user["sub"]
    return result


@app.get("/api/status")
async def get_status(user: dict = Depends(require(READ))) -> dict:
    jarvis = await get_jarvis()
    status = await jarvis._handle_status()
    status["authenticated_as"] = user["sub"]
    return status


@app.get("/api/status/agent_loop")
async def get_agent_loop_status(user: dict = Depends(require(READ))) -> dict:
    jarvis = await get_jarvis()
    return jarvis.get_agent_loop_status()


# ── VPS ────────────────────────────────────────────────────────────────────

@app.get("/api/vps/health", response_model=VPSHealthResponse)
async def vps_health(user: dict = Depends(require(READ))) -> VPSHealthResponse:
    from jarvis.vps_controller import VPS_IP, VPSController
    result = await VPSController().health_check()
    return VPSHealthResponse(
        vps=result.get("vps", VPS_IP),
        reachable=result.get("status") == "ok",
        checks=result.get("checks"),
    )


@app.post("/api/vps/restart")
async def vps_restart(req: VPSRestartRequest, user: dict = Depends(require(VPS_RESTART))) -> dict:
    from jarvis.vps_controller import RESTARTABLE_ENGINES, VPSController
    if req.engine not in RESTARTABLE_ENGINES:
        raise HTTPException(
            status_code=400,
            detail=f"Restart not available for '{req.engine}'. Allowed: {', '.join(RESTARTABLE_ENGINES)}",
        )
    log.warning("vps_restart_requested", engine=req.engine, by=user["sub"])
    return await VPSController().execute(f"restart {req.engine}")


@app.get("/api/vps/engines")
async def vps_engines(user: dict = Depends(require(READ))) -> dict:
    from jarvis.vps_controller import SAFE_COMMANDS, VPS_ENGINES, VPS_IP
    return {"engines": list(VPS_ENGINES.keys()), "vps": VPS_IP, "safe_commands": SAFE_COMMANDS}


# ── Errors ─────────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    error_id = secrets.token_hex(4)
    log.error("unhandled_exception", error_id=error_id, path=request.url.path, error=repr(exc))
    content: dict[str, Any] = {"status": "error", "error_id": error_id, "path": request.url.path}
    # Only callers on the VPS itself see the raw error text
    content["error"] = str(exc) if is_direct_local(request) else "Internal error (see server log)"
    return JSONResponse(status_code=500, content=content)


# ── Entry point ────────────────────────────────────────────────────────────

def run_server(host: str = "127.0.0.1", port: int = 8081, reload: bool = False) -> None:
    uvicorn.run("jarvis.api.server:app", host=host, port=port, reload=reload, log_level="info")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="JARVIS API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (keep loopback; expose via tunnel)")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--reload", action="store_true", help="Auto-reload (dev only)")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, reload=args.reload)
