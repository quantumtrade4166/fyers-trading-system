"""
zerodha_auto_login.py
Fully automated Kite Connect token generation using TOTP — no browser, no
manual request_token paste. Mirrors auth/auto_login.py (Fyers).

Flow:
  1. POST kite.zerodha.com/api/login   (user_id + password) -> request_id
  2. POST kite.zerodha.com/api/twofa   (TOTP)               -> session cookies
  3. GET  connect/login?api_key=...    follow redirects     -> request_token
  4. kite.generate_session(request_token, api_secret)        -> access_token

Writes deployment/zerodha_token.json = {"access_token": ..., "date": "YYYY-MM-DD"}.

Requires in .env:
  KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_KEY     = os.getenv("KITE_API_KEY", "")
API_SECRET  = os.getenv("KITE_API_SECRET", "")
USER_ID     = os.getenv("KITE_USER_ID", "")
PASSWORD    = os.getenv("KITE_PASSWORD", "")
TOTP_SECRET = os.getenv("KITE_TOTP_SECRET", "")
TOKEN_FILE  = Path(__file__).resolve().parent.parent / "zerodha_token.json"

_LOGIN = "https://kite.zerodha.com/api/login"
_TWOFA = "https://kite.zerodha.com/api/twofa"


def is_auto_configured() -> bool:
    return all([API_KEY, API_SECRET, USER_ID, PASSWORD, TOTP_SECRET])


def has_valid_token() -> bool:
    if not TOKEN_FILE.exists():
        return False
    try:
        d = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        return d.get("date") == date.today().isoformat() and bool(d.get("access_token"))
    except Exception:
        return False


def _capture_request_token(session, api_key: str) -> str:
    """Walk the connect/login redirect chain to pull out request_token."""
    import requests
    url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    for _ in range(10):
        try:
            r = session.get(url, allow_redirects=False, timeout=15)
        except requests.exceptions.ConnectionError as e:
            # final hop is the (possibly unreachable) redirect_uri — token is in its URL
            loc = str(getattr(e.request, "url", ""))
            if "request_token=" in loc:
                return parse_qs(urlparse(loc).query)["request_token"][0]
            raise
        loc = r.headers.get("Location", "")
        if "request_token=" in loc:
            return parse_qs(urlparse(loc).query)["request_token"][0]
        if not loc:
            raise RuntimeError("No request_token in redirect chain (login likely failed)")
        url = loc if loc.startswith("http") else "https://kite.zerodha.com" + loc
    raise RuntimeError("Too many redirects while fetching request_token")


def auto_login() -> str:
    if not is_auto_configured():
        raise RuntimeError("Missing KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET (+ api key/secret) in .env")

    import requests
    import pyotp
    from kiteconnect import KiteConnect

    print(f"[zerodha_auto_login] Logging in as {USER_ID}...")
    session = requests.Session()

    r = session.post(_LOGIN, data={"user_id": USER_ID, "password": PASSWORD}, timeout=15)
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"login failed: {body}")
    request_id = body["data"]["request_id"]
    print("  Step 1 OK — password accepted")

    otp = pyotp.TOTP(TOTP_SECRET).now()
    r = session.post(_TWOFA, data={
        "user_id": USER_ID, "request_id": request_id,
        "twofa_value": otp, "twofa_type": "totp",
    }, timeout=15)
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"twofa failed: {body}")
    print("  Step 2 OK — TOTP verified")

    request_token = _capture_request_token(session, API_KEY)
    print("  Step 3 OK — request_token captured")

    kite = KiteConnect(api_key=API_KEY)
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]
    print("  Step 4 OK — access_token generated")

    TOKEN_FILE.write_text(json.dumps({
        "access_token": access_token,
        "date":         date.today().isoformat(),
    }, indent=2), encoding="utf-8")
    print(f"[zerodha_auto_login] Token saved -> {TOKEN_FILE}")
    return access_token


def ensure_token() -> bool:
    """Generate a token only if today's is missing. Returns True if a valid token exists after."""
    if has_valid_token():
        return True
    if not is_auto_configured():
        print("[zerodha_auto_login] Auto-login not configured — skipping.")
        return False
    try:
        auto_login()
        return True
    except Exception as e:
        print(f"[zerodha_auto_login] FAILED: {e}")
        return False


if __name__ == "__main__":
    try:
        auto_login()
        print("[zerodha_auto_login] Done.")
    except Exception as e:
        print(f"[zerodha_auto_login] FAILED: {e}")
        sys.exit(1)
