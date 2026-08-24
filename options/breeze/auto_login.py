"""Headless daily Breeze login — the counterpart to auth/auto_login.py (Fyers).

The README used to say Breeze has no headless flow. That was true only in the
sense that nobody had built one; the login is a plain ASP.NET form with no
captcha, so it can be driven directly. Probed 2026-08-25:

    1. GET  /apiuser/login?api_key=...      -> bounce form (AppKey, time_stamp,
                                               checksum)
    2. POST /apiuser/tradelogin             -> the real login page, which carries
                                               an RSA public key in #hidpv
    3. RSA-encrypt the password with that key (JSEncrypt = PKCS#1 v1.5) into
       the hidden field `hidp`; the plaintext box is never sent
    4. POST /apiuser/tradelogin/getotp      -> triggers the 2FA step
    5. POST the 6-digit code                -> redirects to
                                               https://127.0.0.1/?apisession=NNNN

Step 5 is the one assumption that only a real run can settle: the page calls it
"OTP", and whether the account's 2FA is an authenticator app (scriptable, what
BREEZE_TOTP_SECRET is for) or an SMS code (not scriptable by anything) depends
on how the account is configured. If it turns out to be SMS, this script cannot
work and the daily step stays manual — run with --debug to see which it is.

Usage:
    python -m options.breeze.auto_login            # log in, save the token
    python -m options.breeze.auto_login --debug    # verbose, dumps each step
    python -m options.breeze.auto_login --check    # is today's token still good?
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import base64
import re
import urllib.parse

import requests

from options.breeze.config import get_credentials, get_login_credentials
from options.breeze.session import save_token

BASE = "https://api.icicidirect.com/apiuser"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _inputs(html: str) -> dict:
    """Every <input name=... value=...> on the page, as a dict."""
    out = {}
    for tag in re.findall(r"<input[^>]*>", html, re.I):
        name = re.search(r"name=['\"]([^'\"]+)['\"]", tag, re.I)
        if not name:
            continue
        val = re.search(r"value=['\"]([^'\"]*)['\"]", tag, re.I)
        out[name.group(1)] = val.group(1) if val else ""
    return out


def _rsa_encrypt(public_key_pem: str, plaintext: str) -> str:
    """Mirror JSEncrypt: RSA PKCS#1 v1.5, base64-encoded."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_public_key(public_key_pem.strip().encode())
    return base64.b64encode(
        key.encrypt(plaintext.encode(), padding.PKCS1v15())
    ).decode()


def _extract_session(text: str, url: str) -> str | None:
    """Pull apisession out of a redirect URL or a form field."""
    for candidate in (url, text):
        if not candidate:
            continue
        m = re.search(r"[?&](?:apisession|API_Session)=(\d+)", candidate, re.I)
        if m:
            return m.group(1)
    m = re.search(r"name=['\"]API_Session['\"][^>]*value=['\"](\d+)", text, re.I)
    return m.group(1) if m else None


def login(debug: bool = False) -> str:
    api_key, _ = get_credentials()
    user_id, password, totp_secret = get_login_credentials()

    import pyotp

    s = requests.Session()
    s.headers["User-Agent"] = UA

    # --- 1. bounce page -----------------------------------------------------
    r1 = s.get(f"{BASE}/login?api_key={urllib.parse.quote_plus(api_key)}", timeout=30)
    r1.raise_for_status()
    handshake = _inputs(r1.text)
    if debug:
        print(f"[1] bounce  {r1.status_code}  fields={list(handshake)}")

    # --- 2. real login page -------------------------------------------------
    r2 = s.post(f"{BASE}/tradelogin", data=handshake, timeout=30)
    r2.raise_for_status()
    fields = _inputs(r2.text)
    pub_key = fields.get("ctl00$ContentPlaceHolder1$hidpv", "")
    if not pub_key.strip().startswith("-----BEGIN"):
        raise RuntimeError(
            "No RSA public key on the login page — the flow has changed. "
            "Re-probe before trusting this script."
        )
    if debug:
        print(f"[2] login page  {r2.status_code}  rsa key {len(pub_key)} chars")

    # --- 3. encrypt the password exactly as the browser does ----------------
    payload = dict(fields)
    payload["User/Login Id"] = user_id
    payload["hidp"] = _rsa_encrypt(pub_key, password)
    payload["Password"] = "************"      # what the page actually posts
    payload["chkssTnc"] = "on"                 # terms checkbox
    payload.pop("hiddob", None)

    # --- 4. trigger 2FA -----------------------------------------------------
    r3 = s.post(f"{BASE}/tradelogin/getotp", data=payload, timeout=30)
    if debug:
        print(f"[3] getotp  {r3.status_code}  len={len(r3.text)}")
        print("    body:", re.sub(r"\s+", " ", r3.text[:400]))

    low = r3.text.lower()
    if "sms" in low or "mobile" in low or "registered number" in low:
        print("\n⚠ This account's 2FA looks like an SMS/mobile OTP, not an "
              "authenticator app.\n  No script can read an SMS — the daily "
              "login stays manual.\n  Switch the account to TOTP in ICICI's "
              "settings to make this work.")

    # --- 5. submit the authenticator code -----------------------------------
    code = pyotp.TOTP(totp_secret).now()
    otp_payload = dict(payload)
    otp_payload.update({"otp": code, "OTP": code, "txtOTP": code})
    for i, digit in enumerate(code, start=1):       # the page splits it per box
        otp_payload[f"otp{i}"] = digit

    r4 = s.post(f"{BASE}/tradelogin/validateuser", data=otp_payload,
                timeout=30, allow_redirects=True)
    if debug:
        print(f"[4] validate  {r4.status_code}  url={r4.url[:90]}")
        print("    body:", re.sub(r"\s+", " ", r4.text[:400]))

    token = _extract_session(r4.text, r4.url)
    if not token:
        # The redirect to 127.0.0.1 fails to connect, so the token can also
        # surface in the history chain rather than the final response.
        for hop in r4.history:
            token = _extract_session("", hop.headers.get("Location", ""))
            if token:
                break

    if not token:
        raise RuntimeError(
            "Logged in but no apisession token came back.\n"
            "Re-run with --debug and read step [4] — the usual causes are a "
            "wrong password, an SMS-based 2FA, or a changed form."
        )
    return token


def main() -> int:
    ap = argparse.ArgumentParser(description="Headless daily Breeze login")
    ap.add_argument("--debug", action="store_true", help="dump each step")
    ap.add_argument("--check", action="store_true",
                    help="only report whether today's token is valid")
    args = ap.parse_args()

    if args.check:
        from options.breeze.session import load_token
        try:
            load_token()
            print("✅ Breeze session token is valid for today.")
            return 0
        except Exception as exc:
            print(f"❌ {exc}")
            return 1

    try:
        token = login(debug=args.debug)
    except Exception as exc:
        print(f"❌ Breeze auto-login failed: {exc}")
        return 1

    save_token(token)
    print("✅ Breeze auto-login succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
