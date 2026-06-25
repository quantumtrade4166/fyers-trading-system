"""
core/fyers_client.py
====================

Shared authenticated Fyers client for the strangle strategy.

IMPORTANT: this reads the token string directly and NEVER triggers an
interactive/browser login. Generating a token on the local machine would
invalidate the VPS token and kill the live feed. The VPS regenerates the only
valid token each morning at 9:00 AM; copy it locally with
`fetch_fyers_token_VPS.bat` when local API access is needed.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import base64
import datetime as dt
from pathlib import Path

from fyers_apiv3 import fyersModel

ROOT       = Path(__file__).resolve().parents[3]      # G:\fyers_data_pipeline
CLIENT_ID  = "W09OMXQB8J-100"
TOKEN_FILE = ROOT / "config" / "access_token.txt"


def token_status() -> dict:
    """Return {date, valid, exp_ist} without raising — for health checks/logging."""
    payload = json.loads(TOKEN_FILE.read_text())
    tok = payload["token"]
    pad = lambda s: s + "=" * (-len(s) % 4)
    claims = json.loads(base64.urlsafe_b64decode(pad(tok.split(".")[1])))
    exp = dt.datetime.fromtimestamp(claims["exp"], dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    return {
        "date":    payload.get("date"),
        "valid":   now < exp,
        "exp_ist": (exp + dt.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M IST"),
    }


def load_raw_token() -> str:
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(f"No token file at {TOKEN_FILE}")
    return json.loads(TOKEN_FILE.read_text())["token"]


def get_client() -> fyersModel.FyersModel:
    return fyersModel.FyersModel(
        client_id=CLIENT_ID, token=load_raw_token(),
        log_path=str(ROOT / "logs"), is_async=False,
    )
