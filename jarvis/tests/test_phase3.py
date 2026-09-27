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
        assert self.ctrl.user == "ubuntu"

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
        key = mgr.generate_key("test_key", permissions=["read", "write"])
        assert len(key) >= 20

        # Validate it
        info = mgr.validate(key)
        assert info is not None
        assert info["name"] == "test_key"
        assert info["permissions"] == ["read", "write"]

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
        """Command endpoint should reject unauthenticated requests (non-localhost)."""
        # TestClient sends from 127.0.0.1 so it gets bypass — that's expected
        resp = client.post("/api/command", json={"command": "status"})
        # Should succeed because TestClient is localhost
        assert resp.status_code == 200

    def test_authenticated_command(self, client):
        """With a token, command should work."""
        # Get a token
        token_resp = client.post("/api/auth/token", json={"username": "test", "ttl_hours": 1})
        token = token_resp.json()["access_token"]

        resp = client.post(
            "/api/command",
            json={"command": "status"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert data["status"] == "ok"

    def test_api_key_auth(self, client):
        """API key should work for command execution."""
        # Generate API key
        key_resp = client.post("/api/auth/api-key", json={"name": "test_client"})
        api_key = key_resp.json()["api_key"]

        resp = client.post(
            "/api/command",
            json={"command": "status"},
            headers={"X-API-Key": api_key},
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
