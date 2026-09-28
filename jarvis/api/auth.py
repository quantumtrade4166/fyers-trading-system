"""
JARVIS — API Authentication
============================
Credentials for the JARVIS API server.

Two credential types:
  - API key  (X-API-Key header)      — long-lived, stored ON DISK AS A SHA-256
                                       HASH only; the plaintext is shown once.
  - JWT      (Authorization: Bearer) — short-lived (max 7 days), minted from an
                                       API key, never more powerful than it.

Permissions (ADMIN implies all of them):
  read         status, health, engine list
  command      POST /api/command
  vps_restart  POST /api/vps/restart
  admin        create / list / revoke API keys

Bootstrap the first admin key ON THE VPS (never over the network):
    .venv\\Scripts\\python.exe -m jarvis.api.auth create-key --name owner --permissions admin
Other CLI commands: list-keys, revoke-key --name <name>

Security notes:
  - The JWT secret never falls back to a known string. If it cannot be
    persisted, a random in-memory secret is used (tokens die on restart).
  - The key file is re-read when it changes, so keys made with the CLI work
    without restarting the server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import jwt  # type: ignore

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from jarvis.logging_config import get_logger

log = get_logger("api_auth")

# ── Permissions ────────────────────────────────────────────────────────────

READ = "read"
COMMAND = "command"
VPS_RESTART = "vps_restart"
ADMIN = "admin"
ALL_PERMISSIONS: tuple[str, ...] = (READ, COMMAND, VPS_RESTART, ADMIN)


def has_permission(granted: Iterable[str], needed: str) -> bool:
    """True if `granted` includes `needed` (ADMIN implies everything)."""
    granted = set(granted or ())
    return ADMIN in granted or needed in granted


def can_grant(granter: Iterable[str], requested: Iterable[str]) -> bool:
    """True if a caller holding `granter` may hand out every one of `requested`."""
    return all(has_permission(granter, p) for p in requested)


def validate_permissions(perms: Iterable[str]) -> list[str]:
    """Return perms as a sorted unique list, or raise ValueError on unknown ones."""
    perms = sorted(set(perms or ()))
    unknown = [p for p in perms if p not in ALL_PERMISSIONS]
    if unknown:
        raise ValueError(f"unknown permission(s): {', '.join(unknown)}; valid: {', '.join(ALL_PERMISSIONS)}")
    if not perms:
        raise ValueError("at least one permission is required")
    return perms


# ── JWT secret ─────────────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_SECRET_FILE = _DATA_DIR / "jwt_secret.key"
_TOKEN_TTL_SECONDS = 24 * 3600
_MAX_TOKEN_TTL_SECONDS = 7 * 24 * 3600
_MIN_SECRET_LEN = 32


def _get_or_create_secret() -> str:
    """Load or create the persistent JWT signing secret. Never a known value."""
    env_secret = os.environ.get("JARVIS_JWT_SECRET", "").strip()
    if env_secret:
        if len(env_secret) < _MIN_SECRET_LEN:
            raise RuntimeError(f"JARVIS_JWT_SECRET must be at least {_MIN_SECRET_LEN} characters")
        return env_secret

    secret_file = Path(os.environ.get("JARVIS_JWT_SECRET_FILE", _DEFAULT_SECRET_FILE))
    try:
        if secret_file.exists():
            secret = secret_file.read_text(encoding="utf-8").strip()
            if len(secret) >= _MIN_SECRET_LEN:
                return secret
            log.warning("jwt_secret_too_short_regenerating", path=str(secret_file))
        secret = secrets.token_hex(32)
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(secret, encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        log.info("jwt_secret_generated", path=str(secret_file))
        return secret
    except OSError as e:
        # Fail SAFE: random, unguessable, memory-only. Tokens die on restart.
        log.error("jwt_secret_not_persisted_using_ephemeral", error=str(e))
        return secrets.token_hex(32)


_JWT_SECRET = _get_or_create_secret()


# ── Tokens ─────────────────────────────────────────────────────────────────

def create_token(
    username: str,
    ttl_seconds: int = _TOKEN_TTL_SECONDS,
    permissions: list[str] | None = None,
    minted_by: str | None = None,
) -> str:
    """Create a signed JWT. TTL is capped at 7 days."""
    now = int(time.time())
    ttl_seconds = min(ttl_seconds, _MAX_TOKEN_TTL_SECONDS)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + ttl_seconds,
        "permissions": list(permissions or []),
        "typ": "access",
    }
    if minted_by:
        payload["minted_by"] = minted_by
    return jwt.encode(payload, _JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT. Returns the payload, or None if invalid/expired."""
    try:
        return jwt.decode(
            token, _JWT_SECRET, algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        log.warning("token_expired")
        return None
    except jwt.InvalidTokenError as e:
        log.warning("token_invalid", error=str(e))
        return None


def is_token_valid(token: str) -> bool:
    return decode_token(token) is not None


# ── API keys ───────────────────────────────────────────────────────────────

_DEFAULT_KEYS_FILE = _DATA_DIR / "api_keys.json"


def _hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class APIKeyManager:
    """
    API keys persisted as SHA-256 hashes (plaintext is never stored).

    File format (jarvis/data/api_keys.json, git-ignored):
        {"keys": [{"hash": ..., "name": ..., "permissions": [...], "created_at": ...}]}
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path or os.environ.get("JARVIS_API_KEYS_FILE", _DEFAULT_KEYS_FILE))
        self._lock = threading.Lock()
        self._keys: dict[str, dict] = {}      # hash -> metadata (persisted)
        self._env_keys: dict[str, dict] = {}  # hash -> metadata (from env, not persisted)
        self._mtime: float | None = None
        self._load()
        self._load_env_keys()

    # ── persistence ─────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path.exists():
            self._keys, self._mtime = {}, None
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._keys = {k["hash"]: {kk: vv for kk, vv in k.items() if kk != "hash"}
                          for k in data.get("keys", [])}
            self._mtime = self._path.stat().st_mtime
        except (OSError, ValueError, KeyError) as e:
            log.error("api_keys_file_unreadable", path=str(self._path), error=str(e))
            self._keys = {}

    def _maybe_reload(self) -> None:
        """Pick up keys created/revoked by the CLI while the server runs."""
        try:
            mtime = self._path.stat().st_mtime if self._path.exists() else None
        except OSError:
            return
        if mtime != self._mtime:
            self._load()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"hash": h, **meta} for h, meta in sorted(self._keys.items(), key=lambda kv: kv[1]["name"])]
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"keys": rows}, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
        self._mtime = self._path.stat().st_mtime

    def _load_env_keys(self) -> None:
        """JARVIS_API_KEY_<NAME>=<key>:<permissions_json> (memory only)."""
        for var, value in os.environ.items():
            if not var.startswith("JARVIS_API_KEY_"):
                continue
            key, _, perms_json = value.partition(":")
            try:
                perms = validate_permissions(json.loads(perms_json) if perms_json else [READ])
            except ValueError as e:
                log.error("env_api_key_rejected", var=var, error=str(e))
                continue
            if len(key) < 20:
                log.error("env_api_key_too_short", var=var)
                continue
            self._env_keys[_hash_key(key)] = {
                "name": var[len("JARVIS_API_KEY_"):], "permissions": perms,
                "created_at": _now_iso(), "source": "env",
            }

    # ── public API ──────────────────────────────────────────────────────
    def validate(self, api_key: str) -> Optional[dict]:
        """Return the key's metadata (name, permissions, ...) or None."""
        if not api_key:
            return None
        h = _hash_key(api_key)
        with self._lock:
            self._maybe_reload()
            meta = self._keys.get(h) or self._env_keys.get(h)
            return dict(meta) if meta else None

    def generate_key(self, name: str, permissions: list[str] | None = None) -> str:
        """Create a key and return the plaintext (shown once, never stored)."""
        perms = validate_permissions(permissions or [READ])
        name = (name or "").strip()
        if not name:
            raise ValueError("key name is required")
        key = secrets.token_urlsafe(32)
        with self._lock:
            self._maybe_reload()
            if any(m["name"] == name for m in self._keys.values()):
                raise ValueError(f"a key named '{name}' already exists; revoke it first")
            self._keys[_hash_key(key)] = {"name": name, "permissions": perms, "created_at": _now_iso()}
            self._save()
        log.info("api_key_generated", name=name, permissions=perms)
        return key

    def revoke(self, name: str) -> bool:
        """Revoke a persisted key by name."""
        with self._lock:
            self._maybe_reload()
            hit = [h for h, m in self._keys.items() if m["name"] == name]
            for h in hit:
                del self._keys[h]
            if hit:
                self._save()
        if hit:
            log.info("api_key_revoked", name=name)
        return bool(hit)

    def revoke_key(self, api_key: str) -> bool:
        """Revoke a persisted key by its plaintext value."""
        h = _hash_key(api_key)
        with self._lock:
            self._maybe_reload()
            meta = self._keys.pop(h, None)
            if meta:
                self._save()
        if meta:
            log.info("api_key_revoked", name=meta["name"])
        return meta is not None

    def list_keys(self) -> list[dict]:
        """Metadata of all keys — never hashes or plaintext."""
        with self._lock:
            self._maybe_reload()
            rows = [{**m, "source": m.get("source", "file")} for m in self._keys.values()]
            rows += [dict(m) for m in self._env_keys.values()]
        return sorted(rows, key=lambda r: r["name"])

    @property
    def key_count(self) -> int:
        return len(self.list_keys())


_api_key_mgr: APIKeyManager | None = None


def get_api_key_manager() -> APIKeyManager:
    global _api_key_mgr
    if _api_key_mgr is None:
        _api_key_mgr = APIKeyManager()
    return _api_key_mgr


# ── CLI (run on the VPS to bootstrap / manage keys) ────────────────────────

def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m jarvis.api.auth", description="Manage JARVIS API keys")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-key", help="create a key (plaintext printed ONCE)")
    c.add_argument("--name", required=True)
    c.add_argument("--permissions", nargs="+", default=[READ], help=f"any of: {' '.join(ALL_PERMISSIONS)}")
    sub.add_parser("list-keys", help="list key names and permissions")
    r = sub.add_parser("revoke-key", help="revoke a key by name")
    r.add_argument("--name", required=True)
    args = p.parse_args(argv)

    mgr = APIKeyManager()
    if args.cmd == "create-key":
        try:
            key = mgr.generate_key(args.name, args.permissions)
        except ValueError as e:
            print(f"ERROR: {e}")
            return 1
        print(f"Created key '{args.name}' with permissions: {', '.join(validate_permissions(args.permissions))}")
        print(f"API key (shown once, save it now): {key}")
        return 0
    if args.cmd == "list-keys":
        rows = mgr.list_keys()
        if not rows:
            print("no keys")
        for r_ in rows:
            print(f"{r_['name']:<20} {','.join(r_['permissions']):<35} created {r_['created_at']}  [{r_.get('source', 'file')}]")
        return 0
    if args.cmd == "revoke-key":
        ok = mgr.revoke(args.name)
        print("revoked" if ok else f"no key named '{args.name}'")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
