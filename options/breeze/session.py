"""Breeze session handling.

Breeze session tokens expire daily and there is no official headless login
(unlike the Fyers TOTP flow in auth/auto_login.py). So the daily step is manual:

    1. python -m options.breeze.session --login     # prints the login URL
    2. Log in in your browser. It lands on https://127.0.0.1/?apisession=NNNNNNNN
       ("This site can't be reached" is expected — the token is in the address bar.)
    3. python -m options.breeze.session --set-token NNNNNNNN

The token is cached in config/breeze_session.json with its date, so downloads
resume without re-logging until the next calendar day.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import json
import urllib.parse
from datetime import date

from options.breeze.config import SESSION_PATH, get_credentials

LOGIN_BASE = "https://api.icicidirect.com/apiuser/login"


def login_url() -> str:
    api_key, _ = get_credentials()
    return f"{LOGIN_BASE}?api_key={urllib.parse.quote_plus(api_key)}"


def save_token(session_token: str) -> None:
    session_token = session_token.strip()
    # Users often paste the whole redirect URL — pull the token out of it.
    if "apisession=" in session_token:
        parsed = urllib.parse.urlparse(session_token)
        qs = urllib.parse.parse_qs(parsed.query)
        session_token = (qs.get("apisession") or qs.get("API_Session") or [""])[0]
    if not session_token:
        raise ValueError("Could not read a session token from that input.")

    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(
        json.dumps({"session_token": session_token, "date": date.today().isoformat()}),
        encoding="utf-8",
    )
    print(f"Saved session token to {SESSION_PATH} (date {date.today().isoformat()})")


def load_token(allow_stale: bool = False) -> str:
    if not SESSION_PATH.exists():
        raise RuntimeError(
            "No Breeze session token yet.\n"
            "Run:  python -m options.breeze.session --login"
        )
    data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    token, token_date = data.get("session_token", ""), data.get("date", "")
    if not token:
        raise RuntimeError(f"{SESSION_PATH} has no session_token.")
    if token_date != date.today().isoformat() and not allow_stale:
        raise RuntimeError(
            f"Breeze session token is from {token_date}, today is {date.today().isoformat()}.\n"
            "Breeze tokens expire daily. Run:  python -m options.breeze.session --login"
        )
    return token


def get_client(allow_stale: bool = False):
    """Return an authenticated BreezeConnect client."""
    from breeze_connect import BreezeConnect

    api_key, api_secret = get_credentials()
    client = BreezeConnect(api_key=api_key)
    client.generate_session(api_secret=api_secret, session_token=load_token(allow_stale))
    return client


def check() -> bool:
    """Verify the session works by making one cheap authenticated call."""
    client = get_client()
    resp = client.get_customer_details(api_session=load_token())
    status = resp.get("Status")
    if status == 200:
        print("Breeze session OK.")
        return True
    print(f"Breeze session FAILED: Status={status} Error={resp.get('Error')}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Breeze session management")
    ap.add_argument("--login", action="store_true", help="print the login URL")
    ap.add_argument("--set-token", metavar="TOKEN",
                    help="save the apisession value (or paste the whole redirect URL)")
    ap.add_argument("--check", action="store_true", help="verify the saved token works")
    args = ap.parse_args()

    try:
        return _dispatch(ap, args)
    except RuntimeError as exc:
        print(f"\n{exc}\n")
        return 1


def _dispatch(ap, args) -> int:
    if args.login:
        print("\nOpen this URL in your browser and log in:\n")
        print(f"    {login_url()}\n")
        print("You'll land on https://127.0.0.1/?apisession=NNNNNNNN")
        print("(\"This site can't be reached\" is normal — the token is in the address bar.)\n")
        print("Then run:")
        print("    python -m options.breeze.session --set-token NNNNNNNN\n")
        return 0

    if args.set_token:
        save_token(args.set_token)
        return 0

    if args.check:
        return 0 if check() else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
