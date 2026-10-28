"""
mcx_terminal/kotak_session.py — the terminal's OWN Kotak P session.
===================================================================

Logs in with the Kotak Neo SDK (consumer key + mobile + UCC + TOTP, then MPIN) and keeps what
the new market feed needs from the validate response: `sid` and `feedUrl`.

A second login on the same key does not end other sessions (tested 2026-10-16), so this can
never knock the strangle / DualMom sessions off. Never prints tokens or credentials.
"""
import logging
import os
import threading
import time
from dataclasses import dataclass

from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.session")

_REQUIRED = ("KOTAK_CONSUMER_KEY", "KOTAK_MOBILE", "KOTAK_UCC", "KOTAK_MPIN", "KOTAK_TOTP_SECRET")


@dataclass
class Session:
    client: object           # neo_api_client.NeoAPI (REST: quotes, orders, scrip master)
    sid: str                 # feed auth token
    feed_url: str            # wss://sfeed.../...
    ucc: str
    at: float                # login time (epoch)


def _ok(r) -> bool:
    return isinstance(r, dict) and not r.get("error") and not r.get("Error Message")


def login() -> Session:
    import pyotp
    from dotenv import load_dotenv
    from neo_api_client import NeoAPI

    from live_trading_options.mcx_terminal import fastrest
    fastrest.install()                       # keep-alive + timeouts for every Kotak REST call in this process
    load_dotenv(C.ROOT / "deployment" / ".env")
    cr = {k: (os.getenv(k + C.ENV_SUFFIX) or "").strip() for k in _REQUIRED}
    missing = [k + C.ENV_SUFFIX for k, v in cr.items() if not v]
    if missing:
        raise RuntimeError("missing in deployment/.env: " + ", ".join(missing))
    mob = cr["KOTAK_MOBILE"].strip('"').strip("'")
    digits = "".join(ch for ch in mob if ch.isdigit())[-10:]
    last = None
    for m in dict.fromkeys([mob, "+91" + digits]):
        c = NeoAPI(environment="prod", access_token=None, neo_fin_key=None,
                   consumer_key=cr["KOTAK_CONSUMER_KEY"])
        r1 = c.totp_login(mobile_number=m, ucc=cr["KOTAK_UCC"],
                          totp=pyotp.TOTP(cr["KOTAK_TOTP_SECRET"].replace(" ", "")).now())
        if not _ok(r1):
            last = f"totp_login: {str(r1.get('error') if isinstance(r1, dict) else r1)[:160]}"
            continue
        r2 = c.totp_validate(mpin=cr["KOTAK_MPIN"])
        if not _ok(r2):
            last = f"totp_validate: {str(r2.get('error') if isinstance(r2, dict) else r2)[:160]}"
            continue
        d = r2.get("data") or {}
        feed = d.get("feedUrl") or ""
        if not feed or not d.get("sid"):
            raise RuntimeError("login OK but no feedUrl/sid in the validate response")
        wss = "wss://" + feed.split("://", 1)[1] if "://" in feed else feed
        log.info("Kotak P login OK, feed host %s", wss.split("/")[2])
        return Session(client=c, sid=d["sid"], feed_url=wss, ucc=cr["KOTAK_UCC"], at=time.time())
    raise RuntimeError(f"Kotak P login failed: {last}")


class SessionHolder:
    """One session for the process; `get(force=True)` re-logs-in (at most every 20 s)."""

    def __init__(self):
        self._s = None
        self._lock = threading.Lock()

    def get(self, force: bool = False) -> Session:
        with self._lock:
            if self._s is None or (force and time.time() - self._s.at > 20):
                self._s = login()
            return self._s
