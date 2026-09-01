"""live/kotak_login_debug.py — show the exact login-step responses (tokens redacted). No orders."""
import sys, os, json, inspect
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from dotenv import load_dotenv
    load_dotenv(REPO / "deployment" / ".env")
except Exception:
    pass

import pyotp
from neo_api_client import NeoAPI


_TOKEN_KEYS = ("token", "sid", "sessionid", "hsserverid", "access", "auth", "jwt", "bearer", "kid")


def redact(o, key=""):
    """Mask ONLY token-like values (by key name). Error messages / codes stay visible —
    they aren't secrets and are what we need to diagnose."""
    if isinstance(o, dict):
        return {k: redact(v, k) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(x, key) for x in o]
    if isinstance(o, str) and any(t in key.lower() for t in _TOKEN_KEYS) and len(o) > 8:
        return f"{o[:4]}…[{len(o)} chars]"
    return o


def show(label, obj):
    print(f"\n{label}:\n" + json.dumps(redact(obj), indent=2, default=str)[:1800], flush=True)


ck = os.getenv("KOTAK_CONSUMER_KEY"); mob = os.getenv("KOTAK_MOBILE")
mpin = os.getenv("KOTAK_MPIN"); secret = os.getenv("KOTAK_TOTP_SECRET")

print("totp_login  sig:", inspect.signature(NeoAPI.totp_login))
print("totp_validate sig:", inspect.signature(NeoAPI.totp_validate))
mpin_v = (mpin or "").strip()
print(f"mpin: len={len(mpin_v)} digits={mpin_v.isdigit()}   secret: len={len((secret or '').strip())}")

print("\n----- totp_login SOURCE -----")
try:
    print(inspect.getsource(NeoAPI.totp_login)[:2500])
except Exception as e:
    print("  (no source)", e)

def fresh_code():
    return pyotp.TOTP((secret or "").strip().replace(" ", "")).now()

# Variant A: mobile only, NO ucc kwarg at all
print("\n===== Variant A: totp_login(mobile_number, totp)  [no ucc] =====")
cA = NeoAPI(environment="prod", access_token=None, neo_fin_key=None, consumer_key=ck)
show("A totp_login ->", cA.totp_login(mobile_number=mob, totp=fresh_code()))
show("A totp_validate ->", cA.totp_validate(mpin=mpin_v))
show("A limits ->", cA.limits())
