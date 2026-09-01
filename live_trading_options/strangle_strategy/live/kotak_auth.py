"""
live/kotak_auth.py — Kotak Neo (v2) login for the strangle MIRROR leg.
======================================================================

Kotak Neo v2 auth needs ONLY a consumer key (no consumer secret). Fully headless
daily login, no manual OTP:

    NeoAPI(consumer_key)  ->  totp_login(mobile, <6-digit code>)  ->  totp_validate(mpin)

The 6-digit code is generated from KOTAK_TOTP_SECRET with pyotp, exactly like the
Fyers/Zerodha auto-logins.

SESSION MODEL: Kotak v2 does NOT document a way to persist a session and reuse it in
a different process, so the design is deliberate: the ENGINE (the only process that
places Kotak orders) calls login() ONCE at startup and holds the client in memory for
the day. A mid-day engine restart simply logs in again with a fresh TOTP code — no
token file, no cross-process sharing.

CREDENTIALS come only from deployment/.env (never hard-coded, never committed):
    KOTAK_CONSUMER_KEY, KOTAK_MOBILE, KOTAK_MPIN, KOTAK_TOTP_SECRET

Standalone check (after the .env is filled on the VPS):
    python live/kotak_auth.py         # logs in, prints OK, places NOTHING
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]                 # G:\fyers_data_pipeline
try:
    from dotenv import load_dotenv
    load_dotenv(REPO / "deployment" / ".env")
except Exception:
    pass

_REQUIRED = ("KOTAK_CONSUMER_KEY", "KOTAK_MOBILE", "KOTAK_MPIN", "KOTAK_TOTP_SECRET")


def _totp_now(secret: str) -> str:
    """Current 6-digit TOTP from the base32 secret seed (spaces stripped)."""
    import pyotp
    return pyotp.TOTP(secret.strip().replace(" ", "")).now()


def _mobile_variants(m: str) -> list:
    """Kotak's docs don't pin down the mobile format, so try the sensible options in
    order: as-given, +91-prefixed, and the bare 10 digits. First that logs in wins."""
    m = (m or "").strip()
    digits = m.lstrip("+")
    out = [m]
    if digits.startswith("91") and len(digits) > 10:
        out += ["+" + digits, digits[-10:]]
    elif len(digits) == 10:
        out += ["+91" + digits, digits]
    seen = set()
    return [x for x in out if x and not (x in seen or seen.add(x))]


def login(verbose: bool = True):
    """Return a logged-in NeoAPI client, or raise RuntimeError. Never places an order.

    Tries the mobile number with/without the +91 country code so a wrong guess there
    can't block the whole integration."""
    from neo_api_client import NeoAPI

    creds = {k: os.getenv(k) for k in _REQUIRED}
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise RuntimeError("Kotak .env missing: " + ", ".join(missing)
                           + "  (add them to deployment/.env on the VPS)")

    last = None
    for mob in _mobile_variants(creds["KOTAK_MOBILE"]):
        try:
            client = NeoAPI(environment="prod", access_token=None, neo_fin_key=None,
                            consumer_key=creds["KOTAK_CONSUMER_KEY"])
            client.totp_login(mobile_number=mob, ucc="", totp=_totp_now(creds["KOTAK_TOTP_SECRET"]))
            client.totp_validate(mpin=creds["KOTAK_MPIN"])
            if verbose:
                print(f"  [kotak] login OK (mobile={mob})", flush=True)
            return client
        except Exception as e:
            last = e
            if verbose:
                print(f"  [kotak] login failed (mobile={mob}): {type(e).__name__}: {e}", flush=True)
    raise RuntimeError(f"Kotak login failed for every mobile format: {last}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    login()
    print("Kotak login OK — session established (no orders placed).")
