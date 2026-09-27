"""
JARVIS — API Authentication
============================
JWT token handling for the JARVIS API server.

Token format:
  - HS256 signed with a secret key
  - Contains: sub (username), exp (expiry), permissions
  - Issued as: POST /api/auth/token

Token validation:
  - Checked on every request via Depends()
  - Returns 401 if expired or invalid

Security:
  - Token stored in memory (not persisted — restart invalidates)
  - Default TTL: 24 hours
  - Users can request tokens for themselves (no separate login page)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import jwt  # type: ignore

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from jarvis.logging_config import get_logger

log = get_logger("api_auth")

# ── Configuration ──────────────────────────────────────────────────────────

_DEFAULT_SECRET_FILE = Path(__file__).resolve().parent.parent / "data" / "jwt_secret.key"
_TOKEN_TTL_SECONDS = 24 * 3600  # 24 hours


def _get_or_create_secret() -> str:
    """Load or generate a persistent JWT secret key."""
    secret_file = Path(os.environ.get("JARVIS_JWT_SECRET", _DEFAULT_SECRET_FILE))
    try:
        if secret_file.exists():
            return secret_file.read_text(encoding="utf-8").strip()
        # Generate new secret
        secret = hashlib.sha256(os.urandom(32)).hexdigest()
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(secret, encoding="utf-8")
        # Restrict permissions on the secret file
        os.chmod(secret_file, 0o600)
        log.info("jwt_secret_generated", path=str(secret_file))
        return secret
    except Exception as e:
        # Fallback to env var or fixed secret (less secure, but works)
        env_secret = os.environ.get("JARVIS_JWT_SECRET", "")
        if env_secret:
            return env_secret
        log.warning("jwt_secret_fallback", error=str(e))
        return "jarvis-dev-secret-CHANGE-IN-PROD"


_JWT_SECRET = _get_or_create_secret()


# ── Token Operations ───────────────────────────────────────────────────────

def create_token(
    username: str,
    ttl_seconds: int = _TOKEN_TTL_SECONDS,
    permissions: list[str] | None = None,
) -> str:
    """
    Create a JWT access token.

    Args:
        username: Who the token is for.
        ttl_seconds: How long the token is valid.
        permissions: Optional list of permission scopes.

    Returns:
        Encoded JWT string.
    """
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + ttl_seconds,
        "permissions": permissions or [],
    }
    token = jwt.encode(payload, _JWT_SECRET, algorithm="HS256")
    return token


def decode_token(token: str) -> Optional[dict]:
    """
    Decode and validate a JWT token.

    Returns:
        Decoded payload dict, or None if invalid/expired.
    """
    try:
        payload = jwt.decode(token, _JWT_SECRET, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        log.warning("token_expired")
        return None
    except jwt.InvalidTokenError as e:
        log.warning("token_invalid", error=str(e))
        return None


def is_token_valid(token: str) -> bool:
    """Quick validity check."""
    payload = decode_token(token)
    return payload is not None


# ── API Key Alternative (for curl / simple clients) ────────────────────────

class APIKeyManager:
    """
    Simple API key manager (alternative to JWT for non-browser clients).

    API keys are stored in memory as a dict of key → permissions.
    In production, move this to a database table.
    """

    def __init__(self) -> None:
        self._keys: dict[str, dict] = {}
        self._load_default_keys()

    def _load_default_keys(self) -> None:
        """Load API keys from environment variables."""
        # JARVIS_API_KEY_<NAME>=<key>:<permissions_json>
        for key, value in os.environ.items():
            if key.startswith("JARVIS_API_KEY_"):
                name = key[len("JARVIS_API_KEY_"):]
                parts = value.split(":", 1)
                api_key = parts[0]
                perms = json.loads(parts[1]) if len(parts) > 1 else ["read"]
                self._keys[api_key] = {
                    "name": name,
                    "permissions": perms,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                log.info("api_key_loaded", name=name)

    def validate(self, api_key: str) -> Optional[dict]:
        """Validate an API key, returning its metadata or None."""
        return self._keys.get(api_key)

    def generate_key(self, name: str, permissions: list[str] | None = None) -> str:
        """Generate a new API key."""
        import secrets
        key = secrets.token_urlsafe(32)
        self._keys[key] = {
            "name": name,
            "permissions": permissions or ["read"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        log.info("api_key_generated", name=name)
        return key

    def revoke_key(self, api_key: str) -> bool:
        """Revoke an API key."""
        if api_key in self._keys:
            name = self._keys[api_key]["name"]
            del self._keys[api_key]
            log.info("api_key_revoked", name=name)
            return True
        return False

    @property
    def key_count(self) -> int:
        return len(self._keys)


# Singleton
_api_key_mgr: APIKeyManager | None = None


def get_api_key_manager() -> APIKeyManager:
    global _api_key_mgr
    if _api_key_mgr is None:
        _api_key_mgr = APIKeyManager()
    return _api_key_mgr
