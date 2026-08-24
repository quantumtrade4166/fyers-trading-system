"""Breeze credentials + paths.

Secrets live in deployment/.env (git-ignored), same as every other broker here.
Required keys:

    BREEZE_API_KEY=...
    BREEZE_API_SECRET=...

The daily session token is NOT stored in .env — it changes every day and is
written to config/breeze_session.json by session.py.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / "deployment" / ".env"
SESSION_PATH = PROJECT_ROOT / "config" / "breeze_session.json"
DATA_DIR = PROJECT_ROOT / "data" / "BREEZE_OPTIONS"
MANIFEST_PATH = DATA_DIR / "download_manifest.json"
PROBE_DIR = PROJECT_ROOT / "options" / "breeze" / "probe_results"

# Documented Breeze limits (api.icicidirect.com). The throttle enforces these.
MAX_CALLS_PER_MINUTE = 100
MAX_CALLS_PER_DAY = 5000
MAX_CANDLES_PER_REQUEST = 1000


def _load_env() -> dict:
    """Minimal .env reader — avoids adding a python-dotenv dependency."""
    values = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def get_login_credentials() -> tuple[str, str, str]:
    """Return (user_id, password, totp_secret) for the headless daily login.

    Separate from get_credentials(): the API key/secret identify the *app*,
    these identify the *account*. Only auto_login.py needs them — every other
    module works off the session token.
    """
    env = _load_env()
    user_id = os.getenv("BREEZE_USER_ID") or env.get("BREEZE_USER_ID", "")
    password = os.getenv("BREEZE_PASSWORD") or env.get("BREEZE_PASSWORD", "")
    totp = os.getenv("BREEZE_TOTP_SECRET") or env.get("BREEZE_TOTP_SECRET", "")

    missing = [n for n, v in (("BREEZE_USER_ID", user_id),
                              ("BREEZE_PASSWORD", password),
                              ("BREEZE_TOTP_SECRET", totp)) if not v]
    if missing:
        raise RuntimeError(
            f"Missing {', '.join(missing)}.\n"
            f"Add them to {ENV_PATH} (the account login, not the API key):\n"
            f"    BREEZE_USER_ID=your_icici_login_id\n"
            f"    BREEZE_PASSWORD=your_password\n"
            f"    BREEZE_TOTP_SECRET=your_authenticator_seed"
        )
    return user_id, password, totp


def get_credentials() -> tuple[str, str]:
    """Return (api_key, api_secret). Env vars win over .env for VPS overrides."""
    env = _load_env()
    api_key = os.getenv("BREEZE_API_KEY") or env.get("BREEZE_API_KEY", "")
    api_secret = os.getenv("BREEZE_API_SECRET") or env.get("BREEZE_API_SECRET", "")

    missing = [n for n, v in (("BREEZE_API_KEY", api_key),
                              ("BREEZE_API_SECRET", api_secret)) if not v]
    if missing:
        raise RuntimeError(
            f"Missing {', '.join(missing)}.\n"
            f"Add them to {ENV_PATH}:\n"
            f"    BREEZE_API_KEY=your_key\n"
            f"    BREEZE_API_SECRET=your_secret"
        )
    return api_key, api_secret
