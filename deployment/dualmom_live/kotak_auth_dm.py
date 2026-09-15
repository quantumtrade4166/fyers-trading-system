"""
dualmom_live/kotak_auth_dm.py — login for the DualMom-DEDICATED Kotak Neo account.

⚠️ THIS IS A DIFFERENT KOTAK ACCOUNT FROM THE STRANGLE'S.
Confirmed by the user 2026-09-07. That is what makes a login here safe: Kotak
allows one session PER ACCOUNT, so a second account has its own session and
cannot disturb the Vwap Strangle mirror.

The credential names are deliberately prefixed `KOTAK_DM_` so they can never be
confused with the strangle's `KOTAK_*` set. Loading the wrong one would log into
the strangle's account and kill its session mid-day — the exact failure this
separation exists to prevent.

    deployment/.env
        KOTAK_DM_CONSUMER_KEY
        KOTAK_DM_MOBILE          +91XXXXXXXXXX
        KOTAK_DM_UCC             Unique Client Code (Kotak app -> Profile)
        KOTAK_DM_MPIN
        KOTAK_DM_TOTP_SECRET

Hard-won details reused from the strangle's kotak_auth.py:
  - the SDK returns error DICTS and never raises, so every step must be checked
    (`_ok`) or a failed login reads as success
  - totp_login REQUIRES mobile AND ucc AND totp together; the UCC is easy to miss
  - mobile format varies, so the sensible variants are tried in order

Standalone check (safe — logs in, prints, places nothing):
    python -m deployment.dualmom_live.kotak_auth_dm
"""

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
try:
    from dotenv import load_dotenv
    load_dotenv(REPO / "deployment" / ".env")
except Exception:
    pass

PREFIX = "KOTAK_DM_"
_REQUIRED = ("CONSUMER_KEY", "MOBILE", "UCC", "MPIN", "TOTP_SECRET")

# credentials that must NEVER be used from here — they belong to the strangle
_FORBIDDEN = ("KOTAK_CONSUMER_KEY", "KOTAK_MOBILE", "KOTAK_UCC",
              "KOTAK_MPIN", "KOTAK_TOTP_SECRET")


def _env(name: str):
    return (os.getenv(PREFIX + name) or "").strip()


def missing() -> list:
    return [PREFIX + k for k in _REQUIRED if not _env(k)]


def _totp_now(secret: str) -> str:
    import pyotp
    return pyotp.TOTP(secret.replace(" ", "")).now()


def _ok(resp) -> bool:
    """The SDK signals failure with a dict, not an exception."""
    return isinstance(resp, dict) and not resp.get("error") and not resp.get("Error Message")


def _mobile_variants(m: str) -> list:
    m = (m or "").strip()
    d = m.lstrip("+")
    out = [m]
    if d.startswith("91") and len(d) > 10:
        out += ["+" + d, d[-10:]]
    elif len(d) == 10:
        out += ["+91" + d, d]
    seen = set()
    return [x for x in out if x and not (x in seen or seen.add(x))]


def login(verbose: bool = True):
    """Return a logged-in NeoAPI client for the DualMom account. Places nothing."""
    gaps = missing()
    if gaps:
        raise RuntimeError(
            "DualMom Kotak credentials missing from deployment/.env: "
            + ", ".join(gaps)
            + "\nThese are for the SEPARATE DualMom account — do NOT reuse the "
              "strangle's KOTAK_* values, which belong to a different account.")

    # guard against a copy-paste that would point us at the strangle's account
    for f in _FORBIDDEN:
        if os.getenv(f) and os.getenv(f).strip() == _env(f[len("KOTAK_"):]):
            raise RuntimeError(
                f"{PREFIX}{f[len('KOTAK_'):]} is identical to {f} — that is the "
                "STRANGLE's account. Logging in would steal its session. Refusing.")

    # Kotak Rohit's whitelisted IP is 103.49.131.3; the VPS default IP belongs to
    # the strangle's account. Pin BEFORE the SDK opens its first connection.
    # Fails closed if that IP is not present on this machine.
    from deployment.dualmom_live import source_ip
    pinned = source_ip.install()

    from neo_api_client import NeoAPI

    client = NeoAPI(environment="prod", consumer_key=_env("CONSUMER_KEY"))
    last = None
    for mob in _mobile_variants(_env("MOBILE")):
        resp = client.totp_login(mobile_number=mob, ucc=_env("UCC"),
                                 totp=_totp_now(_env("TOTP_SECRET")))
        if _ok(resp):
            last = None
            break
        last = resp
    if last is not None:
        raise RuntimeError(f"totp_login failed: {last}")

    resp = client.totp_validate(mpin=_env("MPIN"))
    if not _ok(resp):
        raise RuntimeError(f"totp_validate failed: {resp}")

    if verbose:
        print(f"  [dualmom] Kotak login OK (UCC {_env('UCC')[:4]}***, "
              f"dedicated DualMom account, source IP {pinned})")
    return client


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    gaps = missing()
    if gaps:
        print("credentials not set yet:")
        for g in gaps:
            print(f"  {g}")
        print("\nAdd them to deployment/.env, then re-run. Nothing was contacted.")
        sys.exit(2)
    login()
    print("  login verified — no order placed")
