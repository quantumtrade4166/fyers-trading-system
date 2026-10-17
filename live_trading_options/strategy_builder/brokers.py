"""
strategy_builder/brokers.py — the builder's OWN broker sessions.
================================================================

Accounts (ids are what the page sends back):
    zerodha        Kite — reads the 09:00 token file (deployment/zerodha_token.json). It never
                   logs in itself, so it can never invalidate the strangle's Kite session.
    kotak_bhaiya   Kotak Neo, env keys KOTAK_*
    kotak_rohit    Kotak Neo, env keys KOTAK_*_ROHIT
    kotak_p        Kotak Neo, env keys KOTAK_*_D   ("Kotak P" = the code's "Kotak D")

Kotak: the builder logs in with its OWN session on the account's (only) API key. Tested on
2026-10-16 (tools/vps/kotak_session_test.py): a second login on the same key does NOT end the
first, so this cannot knock the strangle / DualMom sessions off.

The login code reuses kotak_auth's helpers but builds the credential set itself from the
suffix, so live strangle code is not modified to add accounts.

Auto-login: every account with credentials logs in at start-up and again each trading day
after 09:02 IST (the Kite token appears at 09:00). A failed read re-logs-in once.
"""
import datetime as dt
import logging
import os
import sys
import threading
import time

from live_trading_options.strategy_builder import config as C

sys.path.insert(0, str(C.ROOT / "live_trading_options" / "strangle_strategy"))
from live import kotak_auth as ka                       # noqa: E402  (also loads deployment/.env)
from live import kite_executor as kx                    # noqa: E402
from live import kotak_executor as ke                   # noqa: E402

log = logging.getLogger("sb.brokers")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

ACCOUNTS = [
    {"id": "zerodha", "name": "Zerodha (Kite)", "kind": "kite"},
    {"id": "kotak_bhaiya", "name": "Kotak Bhaiya", "kind": "kotak", "suffix": ""},
    {"id": "kotak_rohit", "name": "Kotak Rohit", "kind": "kotak", "suffix": "_ROHIT"},
    {"id": "kotak_p", "name": "Kotak P", "kind": "kotak", "suffix": "_D"},
]


def today_ist() -> str:
    return dt.datetime.now(IST).date().isoformat()


def kotak_login(suffix: str):
    """Logged-in NeoAPI client for the account whose env keys end in `suffix`."""
    from neo_api_client import NeoAPI
    creds = {k: os.getenv(k + suffix) for k in ka._REQUIRED}
    missing = [k + suffix for k, v in creds.items() if not v]
    if missing:
        raise RuntimeError("missing in .env: " + ", ".join(missing))
    last = None
    for mob in ka._mobile_variants(creds["KOTAK_MOBILE"]):
        client = NeoAPI(environment="prod", access_token=None, neo_fin_key=None,
                        consumer_key=creds["KOTAK_CONSUMER_KEY"])
        r1 = client.totp_login(mobile_number=mob, ucc=creds["KOTAK_UCC"],
                               totp=ka._totp_now(creds["KOTAK_TOTP_SECRET"]))
        if not ka._ok(r1):
            last = f"totp_login: {r1.get('error') if isinstance(r1, dict) else r1}"
            continue
        r2 = client.totp_validate(mpin=creds["KOTAK_MPIN"])
        if not ka._ok(r2):
            last = f"totp_validate: {r2.get('error') if isinstance(r2, dict) else r2}"
            continue
        return client
    raise RuntimeError(f"login failed: {last}")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Account:
    def __init__(self, spec: dict):
        self.id, self.name, self.kind = spec["id"], spec["name"], spec["kind"]
        self.suffix = spec.get("suffix", "")
        self.client = None
        self.day = None                  # IST date the session was made for
        self.status = "logged_out"       # logged_in | logged_out | no_creds | error
        self.error = None
        self.funds = None
        self.at = None
        self.lock = threading.RLock()    # one order flow per account at a time

    def has_creds(self) -> bool:
        if self.kind == "kite":
            return bool(os.getenv("KITE_API_KEY"))
        return all(os.getenv(k + self.suffix) for k in ka._REQUIRED)

    def login(self):
        with self.lock:
            if not self.has_creds():
                self.status, self.error, self.client = "no_creds", "credentials not in deployment/.env", None
                return False
            try:
                if self.kind == "kite":
                    c = kx.get_kite()            # raises if the token file is not today's
                    c.profile()
                else:
                    c = kotak_login(self.suffix)
                self.client, self.day, self.status, self.error = c, today_ist(), "logged_in", None
                self.at = dt.datetime.now(IST).strftime("%H:%M:%S")
                self.refresh_funds()
                log.info("%s logged in", self.id)
                return True
            except Exception as e:
                self.client, self.status, self.error = None, "error", f"{type(e).__name__}: {e}"[:300]
                log.warning("%s login failed: %s", self.id, self.error)
                return False

    def ensure(self):
        """A session valid for TODAY, or raise. Never trades on yesterday's session."""
        with self.lock:
            if self.client is None or self.day != today_ist():
                if not self.login():
                    raise RuntimeError(f"{self.name}: {self.error}")
            return self.client

    def refresh_funds(self):
        try:
            if self.kind == "kite":
                m = self.client.margins("equity") or {}
                self.funds = _num(m.get("net"))
            else:
                r = self.client.limits(segment="ALL", exchange="ALL", product="ALL") or {}
                d = r.get("data") if isinstance(r.get("data"), dict) else r
                self.funds = next((_num(d.get(k)) for k in ("Net", "net", "CashBal", "cashBal")
                                   if _num(d.get(k)) is not None), None)
        except Exception as e:
            log.info("%s funds read failed: %s", self.id, e)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "status": self.status,
                "error": self.error, "funds": self.funds, "since": self.at,
                "today": self.day == today_ist()}


class BrokerHub:
    def __init__(self):
        self.accounts = {s["id"]: Account(s) for s in ACCOUNTS}
        self._stop = threading.Event()

    def get(self, aid: str) -> Account:
        if aid not in self.accounts:
            raise KeyError(f"unknown broker {aid}")
        return self.accounts[aid]

    def status(self) -> list:
        return [a.public() for a in self.accounts.values()]

    def login_all(self):
        for a in self.accounts.values():
            if a.has_creds():
                a.login()
            else:
                a.status, a.error = "no_creds", "credentials not in deployment/.env"

    def _loop(self):
        """Log everyone in after 09:02 IST on weekdays, once per day; refresh funds every 5 min."""
        done_day = None
        last_funds = 0.0
        while not self._stop.is_set():
            n = dt.datetime.now(IST)
            if n.weekday() < 5 and n.time() >= dt.time(9, 2) and done_day != n.date():
                self.login_all()
                done_day = n.date()
            if time.time() - last_funds > 300:
                for a in self.accounts.values():
                    if a.status == "logged_in" and a.day == today_ist():
                        a.refresh_funds()
                last_funds = time.time()
            self._stop.wait(20)

    def start(self):
        threading.Thread(target=self.login_all, daemon=True, name="SB-login0").start()
        threading.Thread(target=self._loop, daemon=True, name="SB-brokers").start()


# ── broker-neutral contract lookup ──
def resolve(acct: Account, leg: dict) -> dict:
    """Exact broker contract for a leg, from the broker's own instrument list."""
    c = acct.ensure()
    if acct.kind == "kite":
        r = kx.resolve(c, leg["index"], leg["expiry"], int(leg["strike"]), leg["type"])
        return {"symbol": r["tradingsymbol"], "exchange": r["exchange"], "lot": r["lot_size"], "token": None}
    r = ke.resolve(c, leg["index"], leg["expiry"], int(leg["strike"]), leg["type"])
    return {"symbol": r["trading_symbol"], "exchange": r["exchange_segment"], "lot": r["lot_size"],
            "token": r["token"]}
