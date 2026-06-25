"""
jainam_adapter.py
Jainam positions via the Symphony XTS Connect API (dealer account).

Key facts discovered against the live Jainam endpoint (smpd.jainam.in:3643):
  - Interactive auth: POST /interactive/user/session  (appKey/secretKey/source)
  - Account is a DEALER (isInvestorClient=False) -> positions REQUIRE clientID,
    and the real client code is login result.clientCodes[0] (e.g. "JSR129"),
    NOT the login userID ("JSR129A02").
  - Positions: GET /interactive/portfolio/positions?dayOrNet=NetWise&clientID=...
  - XTS does NOT populate MTM/UnrealizedMTM/RealizedMTM (all 0.00). The only
    real P&L signal is NetAmount (= SellAmount - BuyAmount). Correct total P&L:
        pnl = NetAmount + Quantity * LTP * Multiplier
    LTP for open legs comes from the Market Data API:
        POST /apibinarymarketdata/auth/login   (MD appKey/secretKey)
        POST /apibinarymarketdata/instruments/quotes  (xtsMessageCode 1501)

Requires in .env:
  XTS_BASE_URL, XTS_API_KEY, XTS_API_SECRET, XTS_SOURCE
  XTS_MD_API_KEY, XTS_MD_API_SECRET  (optional — only for live LTP on open legs)
  XTS_CLIENT_ID  (optional override; default = login clientCodes[0])
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
import threading
from datetime import date

from deployment.brokers.base import BrokerAdapter, BrokerSnapshot, Position

BASE_URL      = os.getenv("XTS_BASE_URL", "").rstrip("/")
API_KEY       = os.getenv("XTS_API_KEY", "")
API_SECRET    = os.getenv("XTS_API_SECRET", "")
SOURCE        = os.getenv("XTS_SOURCE", "WEBAPI")
MD_API_KEY    = os.getenv("XTS_MD_API_KEY", "")
MD_API_SECRET = os.getenv("XTS_MD_API_SECRET", "")
CLIENT_OVERRIDE = os.getenv("XTS_CLIENT_ID", "")
DAY_OR_NET    = os.getenv("XTS_DAY_OR_NET", "NetWise")   # NetWise | DayWise

MD_PATH = "/apibinarymarketdata"

# segment string -> market-data numeric id, and -> short display label
_SEG_NUM = {"NSECM": 1, "NSEFO": 2, "NSECD": 3, "BSECM": 11, "BSEFO": 12, "MCXFO": 51}
_SEG_LBL = {"NSECM": "NSE", "NSEFO": "NFO", "BSECM": "BSE", "BSEFO": "BFO",
            "NSECD": "CDS", "MCXFO": "MCX"}

# day-scoped token cache so we don't re-login every poll (avoids HTTP 429)
_lock = threading.Lock()
_cache = {"date": None, "itoken": None, "client_id": None, "mdtoken": None}


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


class JainamAdapter(BrokerAdapter):
    name = "jainam"

    def is_configured(self) -> bool:
        return bool(BASE_URL and API_KEY and API_SECRET)

    def fetch_snapshot(self) -> BrokerSnapshot:
        if not self.is_configured():
            missing = [n for n, v in (("XTS_BASE_URL", BASE_URL),
                                      ("XTS_API_KEY", API_KEY),
                                      ("XTS_API_SECRET", API_SECRET)) if not v]
            return BrokerSnapshot(self.name, status="not_configured",
                                  message="Missing in .env: " + ", ".join(missing))
        return self._safe(self._fetch)

    # ── auth (cached per trading day) ──────────────────────────────────────────
    def _interactive(self, session):
        today = date.today().isoformat()
        with _lock:
            if _cache["date"] == today and _cache["itoken"]:
                return _cache["itoken"], _cache["client_id"]
        r = session.post(f"{BASE_URL}/interactive/user/session",
                         json={"appKey": API_KEY, "secretKey": API_SECRET, "source": SOURCE},
                         timeout=12)
        r.raise_for_status()
        res = r.json().get("result", {})
        token = res.get("token")
        if not token:
            raise RuntimeError("interactive login returned no token")
        client_id = CLIENT_OVERRIDE or (res.get("clientCodes") or [res.get("userID")])[0]
        with _lock:
            _cache.update(date=today, itoken=token, client_id=client_id)
        return token, client_id

    def _md_token(self, session):
        if not (MD_API_KEY and MD_API_SECRET):
            return None
        today = date.today().isoformat()
        with _lock:
            if _cache["date"] == today and _cache["mdtoken"]:
                return _cache["mdtoken"]
        try:
            r = session.post(f"{BASE_URL}{MD_PATH}/auth/login",
                             json={"appKey": MD_API_KEY, "secretKey": MD_API_SECRET, "source": SOURCE},
                             timeout=12)
            token = r.json().get("result", {}).get("token")
            with _lock:
                _cache["mdtoken"] = token
            return token
        except Exception as e:
            print(f"  [jainam] MD login failed (LTP will be 0): {e}")
            return None

    def _ltps(self, session, mdtoken, open_rows) -> dict:
        """instrument_id -> LTP for the given open positions."""
        if not mdtoken or not open_rows:
            return {}
        instruments = [{"exchangeSegment": _SEG_NUM.get(p.get("ExchangeSegment"), 0),
                        "exchangeInstrumentID": int(p["ExchangeInstrumentId"])}
                       for p in open_rows]
        try:
            r = session.post(f"{BASE_URL}{MD_PATH}/instruments/quotes",
                             json={"instruments": instruments, "xtsMessageCode": 1501,
                                   "publishFormat": "JSON"},
                             headers={"authorization": mdtoken}, timeout=12)
            body = r.json()
            out = {}
            for item in body.get("result", {}).get("listQuotes", []):
                d = json.loads(item) if isinstance(item, str) else item
                iid = d.get("ExchangeInstrumentID")
                t = d.get("Touchline") if isinstance(d.get("Touchline"), dict) else {}
                ltp = _f(d.get("LastTradedPrice") if d.get("LastTradedPrice") is not None
                         else (t.get("LastTradedPrice") if t else None))
                # Expiry-day artifact: a settled option's LastTradedPrice becomes the
                # underlying settlement value (e.g. SENSEX 77100). Cap it to the day's
                # session range; if absurd, fall back to the session Low.
                hi = _f(d.get("High"))
                if hi and ltp > max(hi * 3, 1000):
                    ltp = _f(d.get("Low"))
                if iid is not None:
                    out[int(iid)] = ltp
            return out
        except Exception as e:
            print(f"  [jainam] quotes failed (LTP will be 0): {e}")
            return {}

    # ── main fetch ─────────────────────────────────────────────────────────────
    def _fetch(self) -> BrokerSnapshot:
        # self-heal: if the cached token was invalidated elsewhere (XTS allows one
        # session), drop it and re-login once.
        return self._fetch_once(retry=True)

    def _fetch_once(self, retry: bool) -> BrokerSnapshot:
        import requests
        session = requests.Session()
        token, client_id = self._interactive(session)

        # dealerpositions returns the NETTED book that matches the XTS desktop
        # "Net Positions" view (clean per-leg averages); /positions is churn-blended.
        r = session.get(f"{BASE_URL}/interactive/portfolio/dealerpositions",
                        params={"dayOrNet": DAY_OR_NET, "clientID": client_id},
                        headers={"authorization": token, "Content-Type": "application/json"},
                        timeout=12)
        body = r.json()
        if body.get("type") != "success":
            desc = str(body.get("description", "")).lower()
            if retry and any(k in desc for k in
                             ("token", "authoriz", "unauthor", "not logged", "invalid session")):
                with _lock:
                    _cache["itoken"] = None      # force fresh login
                return self._fetch_once(retry=False)
            if "not available" in desc or "no data" in desc or "no position" in desc:
                return BrokerSnapshot(self.name, status="ok", positions=[])
            return BrokerSnapshot(self.name, status="error",
                                  message=str(body.get("description", "positions failed"))[:200])

        rows = (body.get("result") or {}).get("positionList", []) or []
        # MCX excluded entirely — only NSE / NFO / BFO kept (per user); never in P&L.
        rows = [p for p in rows if "MCX" not in str(p.get("ExchangeSegment", "")).upper()]

        # live LTP only needed for genuinely open legs
        open_rows = [p for p in rows if int(_f(p.get("Quantity"))) != 0]
        ltps = self._ltps(session, self._md_token(session), open_rows)

        positions = []
        for p in rows:
            qty  = int(_f(p.get("Quantity")))
            net  = _f(p.get("NetAmount"))
            mult = _f(p.get("Multiplier")) or 1.0
            iid  = int(_f(p.get("ExchangeInstrumentId")))
            ltp  = ltps.get(iid, 0.0) if qty != 0 else 0.0
            pnl  = round(net + qty * ltp * mult, 2)
            if qty == 0 and pnl == 0:
                continue
            seg  = p.get("ExchangeSegment", "")
            side = "LONG" if qty > 0 else "SHORT" if qty < 0 else "FLAT"
            avg  = _f(p.get("BuyAveragePrice")) if qty >= 0 else _f(p.get("SellAveragePrice"))
            positions.append(Position(
                broker="jainam",
                symbol=str(p.get("TradingSymbol", "")).strip(),
                exchange=_SEG_LBL.get(seg, str(seg)),
                product=str(p.get("ProductType", "")),
                side=side,
                qty=qty,
                avg_price=avg,
                ltp=ltp,
                pnl=pnl,
                realised=net if qty == 0 else 0.0,
            ))
        return BrokerSnapshot(self.name, status="ok", positions=positions)
