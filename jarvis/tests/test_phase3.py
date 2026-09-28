"""
JARVIS — Phase 3 Test Suite
=============================
Tests for:
  - Agent loop wired into main.py
  - VPS controller (mocked SSH)
  - API server auth (JWT + API key)
  - Integration: full flow via CLI + API

Runs without requiring a real VPS connection.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Headers Cloudflare always adds to tunnelled requests — makes a TestClient
# request look like it came from the internet through the tunnel.
TUNNEL = {"cf-connecting-ip": "203.0.113.9", "cf-ray": "8c1f-TEST"}


@pytest.fixture(autouse=True)
def _isolated_api_keys(tmp_path, monkeypatch):
    """Every test gets its own empty key file; never touch jarvis/data/api_keys.json."""
    import jarvis.api.auth as auth
    monkeypatch.setenv("JARVIS_API_KEYS_FILE", str(tmp_path / "api_keys.json"))
    monkeypatch.setattr(auth, "_api_key_mgr", None)
    yield
    monkeypatch.setattr(auth, "_api_key_mgr", None)


# ═══════════════════════════════════════════════════════════════════════════
# Agent Loop Integration Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestAgentLoopIntegration:
    """Tests for agent loop wired into JARVIS main."""

    @pytest.mark.asyncio
    async def test_agent_loop_starts_with_jarvis(self):
        """JARVIS initialization should start the agent loop."""
        from jarvis.main import JARVIS
        jarvis = JARVIS(agent_loop_enabled=True)
        init = await jarvis.initialize()
        assert init["status"] in ("initialized", "already_initialized")
        assert init.get("agent_loop") is True
        assert jarvis.agent_loop_running is True
        # Cleanup
        await jarvis._agent_loop.stop()  # type: ignore

    @pytest.mark.asyncio
    async def test_agent_loop_disabled_flag(self):
        """When disabled, agent loop should not start."""
        from jarvis.main import JARVIS
        jarvis = JARVIS(agent_loop_enabled=False)
        init = await jarvis.initialize()
        assert init.get("agent_loop") is False
        assert jarvis.agent_loop_running is False

    @pytest.mark.asyncio
    async def test_status_includes_agent_loop(self):
        """Status response should include agent loop data."""
        from jarvis.main import JARVIS
        jarvis = JARVIS(agent_loop_enabled=True)
        await jarvis.initialize()

        status = await jarvis._handle_status()
        assert "agent_loop" in status
        assert "running" in status["agent_loop"]

        await jarvis._agent_loop.stop()  # type: ignore

    @pytest.mark.asyncio
    async def test_get_agent_loop_status_method(self):
        """get_agent_loop_status should return structured data."""
        from jarvis.main import JARVIS
        jarvis = JARVIS(agent_loop_enabled=False)

        # Before init
        status = jarvis.get_agent_loop_status()
        assert status["running"] is False

        await jarvis.initialize()
        status = jarvis.get_agent_loop_status()
        assert "running" in status

        # Cleanup if loop was started
        if jarvis._agent_loop is not None:
            await jarvis._agent_loop.stop()

    @pytest.mark.asyncio
    async def test_agent_loop_runs_checks_after_init(self):
        """After init, agent loop should produce check results."""
        from jarvis.main import JARVIS
        jarvis = JARVIS(agent_loop_enabled=True)
        await jarvis.initialize()

        # Wait for disk_space check (short interval via direct call)
        if jarvis._agent_loop:
            await jarvis._agent_loop._run_check("disk_space", jarvis._agent_loop._check_disk_space)

        status = jarvis.get_agent_loop_status()
        assert status["checks"] > 0 or "last_results" in status

        await jarvis._agent_loop.stop()  # type: ignore


# ═══════════════════════════════════════════════════════════════════════════
# VPS Controller Tests (mocked)
# ═══════════════════════════════════════════════════════════════════════════

class TestVPSController:
    """Tests for VPS controller — mocked SSH, no real VPS needed."""

    def setup_method(self):
        from jarvis.vps_controller import VPSController, _SAFE_COMMANDS, VPS_IP
        self.VPS_IP = VPS_IP
        self._safe_commands = _SAFE_COMMANDS
        self.ctrl = VPSController()

    def test_safe_commands_exist(self):
        """Should have a reasonable set of safe commands."""
        assert "restart dashboard" in self._safe_commands
        assert "restart all" in self._safe_commands
        assert "check all" in self._safe_commands
        assert "disk" in self._safe_commands
        assert "uptime" in self._safe_commands
        assert "logs dashboard" in self._safe_commands
        assert len(self._safe_commands) >= 10

    def test_vps_controller_instantiation(self):
        """Controller should be creatable without SSH."""
        assert self.ctrl.host == self.VPS_IP
        assert self.ctrl.port == 22
        assert self.ctrl.user == "Administrator"

    def test_local_mode_on_vps(self, monkeypatch):
        """On the VPS, the controller must run locally — never SSH to itself."""
        from jarvis.vps_controller import VPSController
        monkeypatch.setenv("JARVIS_ON_VPS", "1")
        assert VPSController().local is True
        # A different host is always remote, even when running on the VPS
        assert VPSController(host="10.0.0.1").local is False

    def test_health_check_single_call(self, monkeypatch):
        """Health check must make exactly ONE execute call and parse its JSON."""
        import asyncio
        import json
        from jarvis.vps_controller import VPSController
        monkeypatch.setenv("JARVIS_ON_VPS", "1")
        ctrl = VPSController()
        calls = []

        async def fake_execute(cmd):
            calls.append(cmd)
            payload = {"uptime": "5d", "disk": "C: 33 GB free", "memory": "14 GB free",
                       "dashboard": "HTTP 200", "engines": {"pid": 1, "cmd": "python x"}}
            return {"status": "ok", "stdout": json.dumps(payload), "stderr": ""}

        monkeypatch.setattr(ctrl, "execute", fake_execute)
        result = asyncio.run(ctrl.health_check())
        assert len(calls) == 1
        assert result["status"] == "ok"
        assert result["checks"]["dashboard"] == "HTTP 200"
        # A single engine (PowerShell emits an object) is normalised to a list
        assert result["checks"]["engines"] == [{"pid": 1, "cmd": "python x"}]
        assert result["checks"]["engine_count"] == 1

    def test_parse_health_basic(self):
        """Parse a simple health output."""
        from jarvis.vps_controller import VPSController
        sample = """09:15:03 up 42 days, 3:22, 1 user, load average: 0.52, 0.38, 0.41
/dev/sda1       50G   30G   20G   60% /
Mem:           15Gi  10Gi  5Gi    67%
"""
        result = VPSController.parse_health(sample)
        assert "engines" in result
        assert "disk" in result
        assert "memory" in result

    def test_parse_health_with_engines(self):
        """Parse health output with engine status lines."""
        from jarvis.vps_controller import VPSController
        sample = """dashboard: running
dualmom_kite: running
dualmom_kotak: stopped
nifty_pivot: running
"""
        result = VPSController.parse_health(sample)
        assert result["engines"]["dashboard"] == "running"
        assert result["engines"]["dualmom_kotak"] == "stopped"
        assert result["engines"]["nifty_pivot"] == "running"


# ═══════════════════════════════════════════════════════════════════════════
# API Auth Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestAPIAuth:
    """Tests for JWT and API key authentication."""

    def test_create_and_validate_token(self):
        """JWT token roundtrip: create → decode → validate."""
        from jarvis.api.auth import create_token, decode_token, is_token_valid
        token = create_token("testuser", ttl_seconds=3600)
        assert isinstance(token, str)
        assert len(token) > 20

        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "testuser"
        assert "exp" in payload
        assert "iat" in payload

        assert is_token_valid(token) is True

    def test_expired_token_rejected(self):
        """Expired tokens should return None."""
        from jarvis.api.auth import create_token, decode_token
        # Create a token that expired 1 hour ago
        token = create_token("testuser", ttl_seconds=-3600)
        assert decode_token(token) is None

    def test_invalid_token_rejected(self):
        """Garbage tokens should return None."""
        from jarvis.api.auth import decode_token, is_token_valid
        assert decode_token("not.a.valid.token") is None
        assert is_token_valid("garbage") is False

    def test_api_key_manager(self):
        """API key CRUD."""
        from jarvis.api.auth import get_api_key_manager
        mgr = get_api_key_manager()

        # Generate a key
        key = mgr.generate_key("test_key", permissions=["read", "command"])
        assert len(key) >= 20

        # Validate it
        info = mgr.validate(key)
        assert info is not None
        assert info["name"] == "test_key"
        assert info["permissions"] == ["command", "read"]

        # Revoke it
        assert mgr.revoke_key(key) is True
        assert mgr.validate(key) is None

    def test_jwt_secret_persistence(self):
        """JWT secret should be created and reused."""
        from jarvis.api.auth import _get_or_create_secret, _DEFAULT_SECRET_FILE
        secret1 = _get_or_create_secret()
        secret2 = _get_or_create_secret()
        assert secret1 == secret2
        # File should exist
        assert _DEFAULT_SECRET_FILE.exists()

    def test_token_permissions(self):
        """Tokens should carry permissions."""
        from jarvis.api.auth import create_token, decode_token
        perms = ["read", "write", "admin"]
        token = create_token("admin_user", permissions=perms)
        payload = decode_token(token)
        assert payload["permissions"] == perms


# ═══════════════════════════════════════════════════════════════════════════
# API Server Tests (TestClient)
# ═══════════════════════════════════════════════════════════════════════════

class TestAPIServer:
    """Tests for the FastAPI server using TestClient."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient  # type: ignore
        from jarvis.api.server import app
        return TestClient(app)

    def test_root_endpoint(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["system"] == "JARVIS"
        assert data["status"] == "running"

    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "timestamp" in data

    def test_token_endpoint(self, client):
        resp = client.post("/api/auth/token", json={"username": "testuser", "ttl_hours": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == 3600

    def test_command_requires_auth(self, client):
        """Direct local call is trusted; the same call through a tunnel is refused."""
        assert client.post("/api/command", json={"command": "status"}).status_code == 200
        resp = client.post("/api/command", json={"command": "status"}, headers=TUNNEL)
        assert resp.status_code == 401

    def test_authenticated_command(self, client):
        """A token with 'command' permission works through the tunnel."""
        token_resp = client.post(
            "/api/auth/token", json={"username": "test", "ttl_hours": 1, "permissions": ["command"]}
        )
        assert token_resp.status_code == 200
        token = token_resp.json()["access_token"]

        resp = client.post(
            "/api/command",
            json={"command": "status"},
            headers={**TUNNEL, "Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_api_key_auth(self, client):
        """An API key with 'command' permission works through the tunnel."""
        key_resp = client.post("/api/auth/api-key", json={"name": "test_client", "permissions": ["command"]})
        assert key_resp.status_code == 200
        api_key = key_resp.json()["api_key"]

        resp = client.post(
            "/api/command",
            json={"command": "status"},
            headers={**TUNNEL, "X-API-Key": api_key},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_invalid_token_rejected(self, client):
        """Test the auth dependency rejects bad tokens from non-localhost."""
        from fastapi import HTTPException
        from jarvis.api.server import _auth
        # Mock request with non-localhost IP and bad token
        class FakeRequest:
            class FakeClient:
                host = "192.168.1.100"
            client = FakeClient()
            headers = {"authorization": "Bearer invalid.token.here"}
        try:
            _auth(FakeRequest())
            assert False, "Should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 401

    def test_vps_engines_list(self, client):
        resp = client.get("/api/vps/engines")
        assert resp.status_code == 200
        data = resp.json()
        assert "dashboard" in data["engines"]
        assert len(data["safe_commands"]) > 0


# ═══════════════════════════════════════════════════════════════════════════
# API Security — every hole found on 2026-09-28 has a test here
# ═══════════════════════════════════════════════════════════════════════════

class TestAPISecurity:
    """Tunnel traffic must authenticate; permissions are enforced; secrets stay secret."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient  # type: ignore
        from jarvis.api.server import app
        return TestClient(app, raise_server_exceptions=False)

    @staticmethod
    def _key(perms, name=None):
        from jarvis.api.auth import get_api_key_manager
        return get_api_key_manager().generate_key(name or "k_" + "_".join(perms), perms)

    # Hole 1: tunnel traffic looked local and skipped auth
    @pytest.mark.parametrize("method,path,body", [
        ("get", "/api/status", None),
        ("get", "/api/vps/health", None),
        ("get", "/api/vps/engines", None),
        ("post", "/api/command", {"command": "status"}),
        ("post", "/api/vps/restart", {"engine": "dashboard"}),
        ("get", "/api/auth/api-keys", None),
    ])
    def test_tunnel_without_credentials_is_refused(self, client, method, path, body):
        resp = getattr(client, method)(path, headers=TUNNEL, **({"json": body} if body else {}))
        assert resp.status_code == 401

    @pytest.mark.parametrize("header", ["cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded", "cdn-loop"])
    def test_any_proxy_header_removes_local_trust(self, client, header):
        assert client.get("/api/vps/engines", headers={header: "203.0.113.9"}).status_code == 401

    def test_local_trust_can_be_disabled(self, client, monkeypatch):
        monkeypatch.setenv("JARVIS_TRUST_LOCAL", "0")
        assert client.get("/api/vps/engines").status_code == 401

    def test_public_endpoints_stay_public(self, client):
        assert client.get("/", headers=TUNNEL).status_code == 200
        assert client.get("/health", headers=TUNNEL).status_code == 200

    # Hole 2: anyone could mint keys/tokens with any permissions
    def test_minting_needs_credentials(self, client):
        assert client.post("/api/auth/api-key", json={"name": "x"}, headers=TUNNEL).status_code == 401
        assert client.post("/api/auth/token", json={"username": "x"}, headers=TUNNEL).status_code == 401

    def test_only_admin_can_create_keys(self, client):
        read_key = self._key(["read"])
        resp = client.post("/api/auth/api-key", json={"name": "evil", "permissions": ["admin"]},
                           headers={**TUNNEL, "X-API-Key": read_key})
        assert resp.status_code == 403

    def test_token_cannot_exceed_caller(self, client):
        read_key = self._key(["read"])
        h = {**TUNNEL, "X-API-Key": read_key}
        assert client.post("/api/auth/token", json={"username": "u", "permissions": ["command"]}, headers=h).status_code == 403
        assert client.post("/api/auth/token", json={"username": "u", "permissions": ["admin"]}, headers=h).status_code == 403
        ok = client.post("/api/auth/token", json={"username": "u", "permissions": ["read"]}, headers=h)
        assert ok.status_code == 200 and ok.json()["permissions"] == ["read"]

    def test_tokens_cannot_mint_tokens(self, client):
        admin_key = self._key(["admin"])
        tok = client.post("/api/auth/token", json={"username": "u", "permissions": ["read"]},
                          headers={**TUNNEL, "X-API-Key": admin_key}).json()["access_token"]
        resp = client.post("/api/auth/token", json={"username": "u2"},
                           headers={**TUNNEL, "Authorization": f"Bearer {tok}"})
        assert resp.status_code == 403

    def test_unknown_permission_rejected(self, client):
        admin_key = self._key(["admin"])
        resp = client.post("/api/auth/api-key", json={"name": "bad", "permissions": ["root"]},
                           headers={**TUNNEL, "X-API-Key": admin_key})
        assert resp.status_code == 400

    # Hole 3: permissions were never checked
    def test_read_key_cannot_command_or_restart(self, client):
        h = {**TUNNEL, "X-API-Key": self._key(["read"])}
        assert client.get("/api/vps/engines", headers=h).status_code == 200
        assert client.post("/api/command", json={"command": "status"}, headers=h).status_code == 403
        assert client.post("/api/vps/restart", json={"engine": "dashboard"}, headers=h).status_code == 403

    def test_restart_allowlist(self, client):
        h = {**TUNNEL, "X-API-Key": self._key(["vps_restart"])}
        # Engines without a verified-safe restart are refused before anything runs
        for engine in ("dualmom_kite", "all", "jarvis_api", "rm -rf"):
            assert client.post("/api/vps/restart", json={"engine": engine}, headers=h).status_code == 400

    def test_no_command_kills_all_python(self):
        from jarvis.vps_controller import _SAFE_COMMANDS
        assert "restart_server.ps1" in _SAFE_COMMANDS["restart dashboard"]
        for name, cmd in _SAFE_COMMANDS.items():
            assert "Stop-Process -Name python" not in cmd, f"'{name}' would kill every engine"

    # Key lifecycle: persisted as hashes, revocable, visible to a running server
    def test_keys_stored_as_hashes_only(self, tmp_path):
        from jarvis.api.auth import APIKeyManager
        path = tmp_path / "keys.json"
        key = APIKeyManager(path).generate_key("owner", ["admin"])
        text = path.read_text(encoding="utf-8")
        assert key not in text and "owner" in text
        assert APIKeyManager(path).validate(key)["permissions"] == ["admin"]  # survives restart

    def test_cli_created_key_seen_by_running_server(self, tmp_path):
        import os
        from jarvis.api.auth import APIKeyManager
        path = tmp_path / "keys.json"
        server_mgr = APIKeyManager(path)
        cli_key = APIKeyManager(path).generate_key("from_cli", ["read"])
        os.utime(path, (time.time() + 5, time.time() + 5))  # guarantee a new mtime
        assert server_mgr.validate(cli_key) is not None

    def test_admin_can_list_and_revoke(self, client):
        admin = {**TUNNEL, "X-API-Key": self._key(["admin"], name="owner")}
        victim = self._key(["read"], name="victim")
        listing = client.get("/api/auth/api-keys", headers=admin).json()["keys"]
        assert {"owner", "victim"} <= {k["name"] for k in listing}
        assert all("hash" not in k for k in listing)
        assert client.delete("/api/auth/api-key/victim", headers=admin).status_code == 200
        assert client.get("/api/vps/engines", headers={**TUNNEL, "X-API-Key": victim}).status_code == 401

    # Hole 4: JWT secret fell back to a publicly known string
    def test_secret_never_falls_back_to_known_value(self, tmp_path, monkeypatch):
        from jarvis.api.auth import _get_or_create_secret
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("x")
        monkeypatch.delenv("JARVIS_JWT_SECRET", raising=False)
        monkeypatch.setenv("JARVIS_JWT_SECRET_FILE", str(blocker / "jwt.key"))  # parent is a file → unwritable
        s1, s2 = _get_or_create_secret(), _get_or_create_secret()
        assert "CHANGE-IN-PROD" not in s1 and len(s1) == 64 and s1 != s2

    def test_short_env_secret_rejected(self, monkeypatch):
        from jarvis.api.auth import _get_or_create_secret
        monkeypatch.setenv("JARVIS_JWT_SECRET", "short")
        with pytest.raises(RuntimeError):
            _get_or_create_secret()

    def test_forged_token_rejected(self, client):
        import jwt as pyjwt
        forged = pyjwt.encode({"sub": "x", "iat": int(time.time()), "exp": int(time.time()) + 3600,
                               "permissions": ["admin"]}, "jarvis-dev-secret-CHANGE-IN-PROD", algorithm="HS256")
        resp = client.get("/api/vps/engines", headers={**TUNNEL, "Authorization": f"Bearer {forged}"})
        assert resp.status_code == 401

    # Error replies must not leak internals to remote callers
    def test_errors_hidden_from_remote_callers(self, client, monkeypatch):
        import jarvis.api.server as server

        async def boom():
            raise RuntimeError("secret internal detail")

        monkeypatch.setattr(server, "get_jarvis", boom)
        remote = client.get("/api/status", headers={**TUNNEL, "X-API-Key": self._key(["read"])})
        assert remote.status_code == 500 and "secret internal detail" not in remote.text
        local = client.get("/api/status")
        assert local.status_code == 500 and "secret internal detail" in local.text


# ═══════════════════════════════════════════════════════════════════════════
# Integration Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPhase3Integration:
    """End-to-end Phase 3 integration tests."""

    @pytest.mark.asyncio
    async def test_full_init_with_agent_loop(self):
        """JARVIS should initialize with agent loop and respond to commands."""
        from jarvis.main import JARVIS
        jarvis = JARVIS(agent_loop_enabled=True)

        init = await jarvis.initialize()
        assert init["status"] == "initialized"
        assert init["agent_loop"] is True

        # Process a command while agent loop runs
        result = await jarvis.process("status")
        assert result["status"] == "ok"
        assert result["system"] == "JARVIS"
        assert "agent_loop" in result

        await jarvis._agent_loop.stop()  # type: ignore

    @pytest.mark.asyncio
    async def test_agent_loop_check_results_in_status(self):
        """Agent loop check results should appear in status."""
        from jarvis.main import JARVIS
        jarvis = JARVIS(agent_loop_enabled=True)
        await jarvis.initialize()

        # Trigger a check directly
        if jarvis._agent_loop:
            result = jarvis._agent_loop._check_vps()
            assert result.status in ("healthy", "failed", "degraded")

        status = jarvis.get_agent_loop_status()
        assert "last_results" in status or "running" in status

        await jarvis._agent_loop.stop()  # type: ignore

    @pytest.mark.asyncio
    async def test_api_server_full_flow(self):
        """Full flow: token creation + command execution via auth module."""
        from jarvis.api.auth import create_token, decode_token, is_token_valid

        # 1. Create token
        token = create_token("integration_test", ttl_seconds=3600)
        assert len(token) > 20

        # 2. Validate token
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "integration_test"

        # 3. Verify token is valid
        assert is_token_valid(token) is True

        # 4. Test invalid tokens are rejected
        assert decode_token("garbage") is None
        assert is_token_valid("invalid") is False

    def test_vps_controller_command_resolution(self):
        """All safe commands should resolve to non-empty shell commands."""
        from jarvis.vps_controller import _SAFE_COMMANDS
        for name, cmd in _SAFE_COMMANDS.items():
            assert cmd, f"Empty command for '{name}'"
            assert len(cmd) >= 2, f"Command too short for '{name}': {cmd}"
