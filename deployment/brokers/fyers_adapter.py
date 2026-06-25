"""
fyers_adapter.py
Real Fyers positions via fyers_apiv3 REST (reuses the existing daily token).
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
from pathlib import Path

from deployment.brokers.base import BrokerAdapter, BrokerSnapshot, Position

ACCESS_TOKEN_PATH = Path(os.getenv(
    "ACCESS_TOKEN_PATH",
    r"G:\fyers_data_pipeline\config\access_token.txt",
))
APP_ID = os.getenv("FYERS_APP_ID", "W09OMXQB8J-100")

_EXCH = {10: "NSE", 11: "MCX", 12: "BSE"}


def _read_token() -> str:
    raw = ACCESS_TOKEN_PATH.read_text(encoding="utf-8").strip()
    try:
        return json.loads(raw)["token"]
    except Exception:
        return raw


class FyersAdapter(BrokerAdapter):
    name = "fyers"

    def is_configured(self) -> bool:
        return ACCESS_TOKEN_PATH.exists() and bool(ACCESS_TOKEN_PATH.read_text(encoding="utf-8").strip())

    def fetch_snapshot(self) -> BrokerSnapshot:
        return self._safe(self._fetch)

    def _fetch(self) -> BrokerSnapshot:
        from fyers_apiv3 import fyersModel
        fyers = fyersModel.FyersModel(client_id=APP_ID, token=_read_token(),
                                      is_async=False, log_path="")
        resp = fyers.positions()
        if not isinstance(resp, dict) or resp.get("s") != "ok":
            msg = (resp or {}).get("message", "positions() failed")
            return BrokerSnapshot(self.name, status="error", message=str(msg)[:200])

        positions = []
        for p in resp.get("netPositions", []) or []:
            net_qty = int(p.get("netQty", 0))
            if net_qty == 0 and float(p.get("pl", 0)) == 0:
                continue  # fully closed, nothing to show
            sym = str(p.get("symbol", "")).replace("NSE:", "").replace("BSE:", "").replace("-EQ", "")
            side = "LONG" if net_qty > 0 else "SHORT" if net_qty < 0 else "FLAT"
            positions.append(Position(
                broker="fyers",
                symbol=sym,
                exchange=_EXCH.get(p.get("exchange"), str(p.get("exchange", ""))),
                product=str(p.get("productType", "")),
                side=side,
                qty=net_qty,
                avg_price=float(p.get("netAvg", 0) or 0),
                ltp=float(p.get("ltp", 0) or 0),
                pnl=float(p.get("pl", 0) or 0),
                realised=float(p.get("realized_profit", 0) or 0),
            ))
        return BrokerSnapshot(self.name, status="ok", positions=positions)
