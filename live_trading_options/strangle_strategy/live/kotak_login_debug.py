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


def redact(o):
    """Mask anything token-like (long strings) so structure/errors show but secrets don't."""
    if isinstance(o, dict):
        return {k: redact(v) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(x) for x in o]
    if isinstance(o, str) and len(o) > 22:
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

client = NeoAPI(environment="prod", access_token=None, neo_fin_key=None, consumer_key=ck)
code = pyotp.TOTP((secret or "").strip().replace(" ", "")).now()
print(f"\nTOTP code generated (len={len(code)})")

show("totp_login() ->", client.totp_login(mobile_number=mob, ucc="", totp=code))
# fresh code for the second step in case it wants its own
code2 = pyotp.TOTP((secret or "").strip().replace(" ", "")).now()
show("totp_validate(mpin) ->", client.totp_validate(mpin=mpin_v))
show("limits() after ->", client.limits())
