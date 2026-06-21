# ============================================================
# auth/fyers_auth.py
# ============================================================

import json
import webbrowser
from pathlib import Path
from datetime import date
from fyers_apiv3 import fyersModel
import sys

sys.path.append(str(Path(__file__).parent.parent))

CLIENT_ID    = "W09OMXQB8J-100"
SECRET_KEY   = "F3SKSM9JSG"
REDIRECT_URI = "https://127.0.0.1"
TOKEN_FILE   = Path("config/access_token.txt")


def generate_auth_url() -> str:
    session = fyersModel.SessionModel(
        client_id=CLIENT_ID,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )
    return session.generate_authcode()


def get_access_token(auth_code: str) -> str:
    session = fyersModel.SessionModel(
        client_id=CLIENT_ID,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    response = session.generate_token()
    if response.get("s") != "ok":
        raise ValueError(f"Token generation failed: {response}")
    return response["access_token"]


def save_token(token: str):
    TOKEN_FILE.parent.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({
        "token": token,
        "date": str(date.today())
    }))
    print(f"Token saved to {TOKEN_FILE}")


def load_token() -> str | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        payload = json.loads(TOKEN_FILE.read_text())
        if payload.get("date") == str(date.today()):
            return payload["token"]
        print("Token expired. Re-authentication needed.")
        return None
    except Exception:
        return None


def get_fyers_client():
    """Call this from anywhere in the pipeline to get an authenticated client."""
    token = load_token()
    if not token:
        token = interactive_login()
    return fyersModel.FyersModel(
        client_id=CLIENT_ID,
        token=token,
        log_path="logs",
        is_async=False,
    )


def interactive_login() -> str:
    url = generate_auth_url()
    print(f"\nOpening Fyers login in browser...")
    webbrowser.open(url)
    print("\nAfter logging in, your browser will show an error page — that is NORMAL.")
    print("Copy the FULL URL from the browser address bar and paste it below.")
    print("It will look like: https://127.0.0.1/?s=ok&code=200&auth_code=eyJ...\n")

    redirect_url = input("Paste full redirect URL here: ").strip()

    from urllib.parse import urlparse, parse_qs
    params = parse_qs(urlparse(redirect_url).query)
    auth_code = params.get("auth_code", params.get("code", [None]))[0]

    if not auth_code:
        raise ValueError(f"Could not find auth_code in: {redirect_url}")

    print("Generating access token...")
    token = get_access_token(auth_code)
    save_token(token)
    print("Authentication successful!")
    return token


if __name__ == "__main__":
    print("=== Fyers Authentication ===\n")
    interactive_login()
    print("\nDone! You can now run the pipeline.")