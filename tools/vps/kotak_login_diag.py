"""
tools/vps/kotak_login_diag.py — why does one Kotak account refuse to log in?
Prints ONLY Kotak's replies per step (never the credentials). Places no order.

    .venv\\Scripts\\python.exe tools\\vps\\kotak_login_diag.py --suffix _D
"""
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "live_trading_options" / "strangle_strategy"))
from live import kotak_auth as ka          # noqa: E402  (loads deployment/.env)
from neo_api_client import NeoAPI          # noqa: E402

sfx = sys.argv[sys.argv.index("--suffix") + 1] if "--suffix" in sys.argv else "_D"
c = {k: (os.getenv(k + sfx) or "") for k in ka._REQUIRED}
print(f"account suffix {sfx!r}: " + ", ".join(f"{k}{sfx} {'set' if v else 'MISSING'}" for k, v in c.items()))
for mob in ka._mobile_variants(c["KOTAK_MOBILE"]):
    shape = "+91 + 10 digits" if mob.startswith("+") else f"{len(mob)} digits, no +91"
    client = NeoAPI(environment="prod", access_token=None, neo_fin_key=None, consumer_key=c["KOTAK_CONSUMER_KEY"])
    r1 = client.totp_login(mobile_number=mob, ucc=c["KOTAK_UCC"], totp=ka._totp_now(c["KOTAK_TOTP_SECRET"]))
    print(f"\nmobile as [{shape}] -> totp_login: {'OK' if ka._ok(r1) else r1}")
    if ka._ok(r1):
        r2 = client.totp_validate(mpin=c["KOTAK_MPIN"])
        print(f"   totp_validate (MPIN): {'OK — LOGGED IN' if ka._ok(r2) else r2}")
        break
