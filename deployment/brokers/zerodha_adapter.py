"""
zerodha_adapter.py
Zerodha positions via Kite Connect (kiteconnect SDK).

Requires in .env:
    KITE_API_KEY
    KITE_API_SECRET        (used only by the daily login helper)
Daily access token is stored in deployment/zerodha_token.json (git-excluded),
written by deployment/brokers/zerodha_login.py.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
from datetime import date
from pathlib import Path

from deployment.brokers.base import BrokerAdapter, BrokerSnapshot, Position

API_KEY    = os.getenv("KITE_API_KEY", "")
TOKEN_FILE = Path(__file__).parent.parent / "zerodha_token.json"


def _read_access_token() -> str:
    if not TOKEN_FILE.exists():
        return ""
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        # token is only valid for the trading day it was generated
        if data.get("date") != date.today().isoformat():
            return ""
        return data.get("access_token", "")
    except Exception:
        return ""


class ZerodhaAdapter(BrokerAdapter):
    name = "zerodha"

    def is_configured(self) -> bool:
        return bool(API_KEY)

    def fetch_snapshot(self) -> BrokerSnapshot:
        if not self.is_configured():
            return BrokerSnapshot(self.name, status="not_configured",
                                  message="KITE_API_KEY missing in .env")
        token = _read_access_token()
        if not token:
            return BrokerSnapshot(self.name, status="error",
                                  message="No valid access token for today — run zerodha_login.py")
        return self._safe(lambda: self._fetch(token))

    def _fetch(self, token: str) -> BrokerSnapshot:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(token)
        data = kite.positions() or {}

        positions = []
        for p in data.get("net", []) or []:
            qty = int(p.get("quantity", 0))
            if qty == 0 and float(p.get("pnl", 0)) == 0:
                continue
            side = "LONG" if qty > 0 else "SHORT" if qty < 0 else "FLAT"
            positions.append(Position(
                broker="zerodha",
                symbol=str(p.get("tradingsymbol", "")),
                exchange=str(p.get("exchange", "")),
                product=str(p.get("product", "")),
                side=side,
                qty=qty,
                avg_price=float(p.get("average_price", 0) or 0),
                ltp=float(p.get("last_price", 0) or 0),
                pnl=float(p.get("pnl", 0) or 0),
                realised=float(p.get("realised", 0) or 0),
            ))
        return BrokerSnapshot(self.name, status="ok", positions=positions)
