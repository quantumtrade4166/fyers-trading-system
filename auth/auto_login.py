"""
auth/auto_login.py
Fully automated Fyers token generation using TOTP — no browser needed.
Reads credentials from environment variables (VPS .env file).
Run at 9:00 AM daily via Task Scheduler on VPS.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
import requests
import pyotp
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

# Load .env from deployment folder (VPS path)
_env_path = Path(__file__).resolve().parent.parent / "deployment" / ".env"
load_dotenv(_env_path)

CLIENT_ID    = os.getenv("FYERS_CLIENT_ID", "")
SECRET_KEY   = os.getenv("FYERS_SECRET_KEY", "")
REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "https://127.0.0.1")
FYERS_ID     = os.getenv("FYERS_USER_ID")
PIN          = os.getenv("FYERS_PIN")
TOTP_SECRET  = os.getenv("FYERS_TOTP_SECRET")
TOKEN_FILE   = Path(os.getenv("ACCESS_TOKEN_PATH",
               str(Path(__file__).resolve().parent.parent / "config" / "access_token.txt")))


def _step1_send_otp(fy_id: str) -> str:
    """Step 1 — initiate login, get request_key."""
    r = requests.post(
        "https://api-t2.fyers.in/vagator/v2/send_login_otp",
        json={"fy_id": fy_id, "app_id": "2"},
        timeout=15,
    )
    data = r.json()
    if data.get("s") != "ok":
        raise RuntimeError(f"send_login_otp failed: {data}")
    return data["request_key"]


def _step2_verify_totp(request_key: str, totp_secret: str) -> str:
    """Step 2 — verify TOTP, get new request_key."""
    otp = pyotp.TOTP(totp_secret).now()
    r = requests.post(
        "https://api-t2.fyers.in/vagator/v2/verify_otp",
        json={"request_key": request_key, "otp": otp},
        timeout=15,
    )
    data = r.json()
    if data.get("s") != "ok":
        raise RuntimeError(f"verify_otp failed: {data}")
    return data["request_key"]


def _step3_verify_pin(request_key: str, pin: str) -> str:
    """Step 3 — verify PIN (plain), get session access_token."""
    r = requests.post(
        "https://api-t2.fyers.in/vagator/v2/verify_pin",
        json={"request_key": request_key, "identity_type": "pin", "identifier": pin},
        timeout=15,
    )
    data = r.json()
    if data.get("s") != "ok":
        raise RuntimeError(f"verify_pin failed: {data}")
    return data["data"]["access_token"]


def _step4_get_auth_code(session_token: str, client_id: str, redirect_uri: str, fy_id: str) -> str:
    """Step 4 — get auth_code using session token as Bearer."""
    app_id_short = client_id.split("-")[0]   # "W09OMXQB8J" from "W09OMXQB8J-100"
    app_type     = client_id.split("-")[1]   # "100"
    r = requests.post(
        "https://api-t1.fyers.in/api/v3/token",
        json={
            "fyers_id":       fy_id,
            "app_id":         app_id_short,
            "redirect_uri":   redirect_uri,
            "appType":        app_type,
            "code_challenge": "",
            "state":          "auto_login",
            "scope":          "",
            "nonce":          "",
            "response_type":  "code",
            "create_cookie":  True,
        },
        headers={"Authorization": f"Bearer {session_token}"},
        timeout=15,
    )
    data = r.json()
    url = data.get("Url", "")
    if "auth_code=" not in url:
        raise RuntimeError(f"auth_code not in response: {data}")
    return url.split("auth_code=")[1].split("&")[0]


def _step5_generate_token(auth_code: str, client_id: str, secret_key: str, redirect_uri: str) -> str:
    """Step 5 — exchange auth_code for access_token."""
    from fyers_apiv3 import fyersModel
    session = fyersModel.SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    resp = session.generate_token()
    if resp.get("s") != "ok":
        raise RuntimeError(f"generate_token failed: {resp}")
    return resp["access_token"]


def auto_login() -> str:
    """Run full automated login flow. Returns access_token."""
    if not FYERS_ID or not PIN or not TOTP_SECRET:
        raise RuntimeError("Missing FYERS_USER_ID, FYERS_PIN, or FYERS_TOTP_SECRET in .env")

    print(f"[auto_login] Logging in as {FYERS_ID}...")

    request_key = _step1_send_otp(FYERS_ID)
    print("  Step 1 OK — OTP sent")

    request_key = _step2_verify_totp(request_key, TOTP_SECRET)
    print("  Step 2 OK — TOTP verified")

    session_token = _step3_verify_pin(request_key, PIN)
    print("  Step 3 OK — PIN verified")

    auth_code = _step4_get_auth_code(session_token, CLIENT_ID, REDIRECT_URI, FYERS_ID)
    print("  Step 4 OK — auth_code obtained")

    token = _step5_generate_token(auth_code, CLIENT_ID, SECRET_KEY, REDIRECT_URI)
    print("  Step 5 OK — access_token generated")

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(
        json.dumps({"token": token, "date": str(date.today())}),
        encoding="utf-8",
    )
    print(f"[auto_login] Token saved to {TOKEN_FILE}")
    return token


if __name__ == "__main__":
    try:
        auto_login()
        print("[auto_login] Done.")
    except Exception as e:
        print(f"[auto_login] FAILED: {e}")
        sys.exit(1)
