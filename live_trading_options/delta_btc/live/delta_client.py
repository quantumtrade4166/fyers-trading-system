"""
live/delta_client.py — signed REST client for Delta Exchange India (orders, positions, wallet).
==============================================================================================

The only module in this package that holds API keys or sends an order.

Keys come from deployment/.env (never from code or chat):
    DELTA_API_KEY=...
    DELTA_API_SECRET=...
The key must have TRADING permission and the VPS IP whitelisted on Delta.

Signing (Delta docs): HMAC-SHA256(secret, method + timestamp + path + query + body),
hex, sent as `signature` with `api-key` and `timestamp` (seconds). A signature older
than 5 s is rejected, so it is generated right before each send.

ORDER SAFETY
  - an order POST is NEVER blindly retried: if the network fails after sending, the
    order may already be live. Instead the client looks the order up by its
    client_order_id and returns whatever Delta says happened.
  - every error comes back as DeltaAPIError with Delta's own `code` (e.g.
    "insufficient_margin"), so the caller can tell a margin refusal from a glitch.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import time
import json
import hmac
import hashlib
import urllib.parse
from pathlib import Path

import socket

import requests
import urllib3.util.connection as _urllib3_connection

# Delta API keys are IP-whitelisted, and only IPv4 addresses are whitelisted. A home PC
# with IPv6 reaches Delta over a (rotating) IPv6 address and gets
# "ip_not_whitelisted_for_api_key", so signed requests are forced onto IPv4.
# Set DELTA_ALLOW_IPV6=1 to turn this off.
if os.environ.get("DELTA_ALLOW_IPV6") != "1":
    _urllib3_connection.allowed_gai_family = lambda: socket.AF_INET

BASE = "https://api.india.delta.exchange"
TESTNET = "https://cdn-ind.testnet.deltaex.org"
_UA = "fyers-pipeline/delta-btc-vwap-live"
REPO = Path(__file__).resolve().parents[3]


class DeltaAPIError(RuntimeError):
    def __init__(self, msg, code=None, context=None, status=None):
        super().__init__(msg)
        self.code = code
        self.context = context
        self.status = status


def load_keys():
    """(key, secret) from the environment, loading deployment/.env if needed."""
    if not os.environ.get("DELTA_API_KEY"):
        try:
            from dotenv import load_dotenv
            load_dotenv(REPO / "deployment" / ".env")
        except Exception:
            pass
    return os.environ.get("DELTA_API_KEY"), os.environ.get("DELTA_API_SECRET")


class DeltaClient:
    def __init__(self, key=None, secret=None, base=BASE, timeout=10):
        if key is None or secret is None:
            key, secret = load_keys()
        if not key or not secret:
            raise DeltaAPIError("DELTA_API_KEY / DELTA_API_SECRET missing in deployment/.env",
                                code="no_keys")
        self.key, self.secret, self.base, self.timeout = key, secret, base, timeout
        self.s = requests.Session()

    # ── transport ────────────────────────────────────────────────────────
    def _headers(self, method, path, query, body):
        ts = str(int(time.time()))
        sig = hmac.new(self.secret.encode(), (method + ts + path + query + body).encode(),
                       hashlib.sha256).hexdigest()
        return {"api-key": self.key, "timestamp": ts, "signature": sig,
                "User-Agent": _UA, "Content-Type": "application/json",
                "Accept": "application/json"}

    def _request(self, method, path, params=None, body=None, retries=None):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        # reads may retry; writes never (an order could already be on the book)
        tries = retries if retries is not None else (3 if method == "GET" else 1)
        last = None
        for i in range(tries):
            try:
                r = self.s.request(method, self.base + path + query, data=payload or None,
                                   headers=self._headers(method, path, query, payload),
                                   timeout=self.timeout)
                try:
                    d = r.json()
                except ValueError:
                    d = {"success": False, "error": {"code": f"http_{r.status_code}",
                                                     "context": r.text[:300]}}
                if r.status_code == 429:
                    wait = float(r.headers.get("X-RATE-LIMIT-RESET", 2000)) / 1000.0
                    last = DeltaAPIError("rate limited", code="rate_limited", status=429)
                    if method == "GET" and i < tries - 1:
                        time.sleep(min(max(wait, 0.5), 10))
                        continue
                    raise last
                if not d.get("success", r.ok):
                    err = d.get("error") or {}
                    if isinstance(err, str):
                        err = {"code": err}
                    raise DeltaAPIError(f"{method} {path}: {err.get('code')} {err.get('context') or ''}",
                                        code=err.get("code"), context=err.get("context"),
                                        status=r.status_code)
                return d.get("result")
            except DeltaAPIError:
                raise
            except Exception as e:                 # network: timeout, reset, DNS
                last = e
                if i < tries - 1:
                    time.sleep(0.5 * (i + 1))
        raise DeltaAPIError(f"{method} {path} failed: {type(last).__name__}: {last}",
                            code="network")

    # ── account ──────────────────────────────────────────────────────────
    def wallet(self):
        return self._request("GET", "/v2/wallet/balances")

    def usd_available(self):
        for w in self.wallet() or []:
            if (w.get("asset_symbol") or "").upper() in ("USD", "USDT"):
                return float(w.get("available_balance") or 0)
        return None

    def positions(self):
        """Every open position: {product_id: signed size in contracts} (short < 0)."""
        out = {}
        for p in self._request("GET", "/v2/positions/margined") or []:
            pid = int(p.get("product_id") or (p.get("product") or {}).get("id") or 0)
            size = int(float(p.get("size") or 0))
            if pid and size:
                out[pid] = out.get(pid, 0) + size
        return out

    def transactions(self, page_size=100):
        """Wallet ledger, newest first: cashflow (premium in/out per fill), commission
        (meta_data: amount_without_gst, gst), deposit, ..."""
        return self._request("GET", "/v2/wallet/transactions", params={"page_size": page_size}) or []

    def raw_positions(self):
        return self._request("GET", "/v2/positions/margined") or []

    # ── leverage (per product, persists on the account) ──────────────────
    def get_leverage(self, product_id):
        r = self._request("GET", f"/v2/products/{int(product_id)}/orders/leverage") or {}
        return float(r.get("leverage")) if r.get("leverage") is not None else None

    def set_leverage(self, product_id, leverage):
        """Order leverage for one contract. Each daily option is a NEW product, so
        this has to be set on every strike before its first sell — otherwise Delta
        applies the default leverage and asks for far more margin."""
        r = self._request("POST", f"/v2/products/{int(product_id)}/orders/leverage",
                          body={"leverage": str(leverage)}) or {}
        return float(r.get("leverage")) if r.get("leverage") is not None else None

    # ── orders ───────────────────────────────────────────────────────────
    def place_order(self, product_id, side, size, *, order_type="limit_order",
                    limit_price=None, tif="ioc", reduce_only=False, client_order_id=None):
        """One order. Returns Delta's order dict. On a network failure the order is
        looked up by client_order_id rather than re-sent."""
        body = {"product_id": int(product_id), "side": side, "size": int(size),
                "order_type": order_type, "reduce_only": bool(reduce_only)}
        if order_type == "limit_order":
            body["limit_price"] = str(limit_price)
            body["time_in_force"] = tif
        if client_order_id:
            body["client_order_id"] = client_order_id
        try:
            return self._request("POST", "/v2/orders", body=body)
        except DeltaAPIError as e:
            if e.code == "network" and client_order_id:
                time.sleep(1.0)
                found = self.order_by_client_id(client_order_id)
                if found:
                    return found
            raise

    def get_order(self, order_id):
        return self._request("GET", f"/v2/orders/{int(order_id)}")

    def order_by_client_id(self, client_order_id):
        try:
            return self._request("GET", f"/v2/orders/client_order_id/{client_order_id}")
        except DeltaAPIError:
            return None

    def cancel(self, order_id, product_id):
        return self._request("DELETE", "/v2/orders",
                             body={"id": int(order_id), "product_id": int(product_id)})

    def open_orders(self, product_ids=None):
        p = {"states": "open,pending"}
        if product_ids:
            p["product_ids"] = ",".join(str(int(x)) for x in product_ids)
        return self._request("GET", "/v2/orders", params=p) or []

    def fills(self, product_ids=None, page_size=100):
        p = {"page_size": page_size}
        if product_ids:
            p["product_ids"] = ",".join(str(int(x)) for x in product_ids)
        return self._request("GET", "/v2/fills", params=p) or []


def order_result(o: dict, requested: int) -> dict:
    """Normalise an order response: filled contracts, average price, commission."""
    o = o or {}
    size = int(float(o.get("size") or requested))
    unfilled = int(float(o.get("unfilled_size") if o.get("unfilled_size") is not None else size))
    filled = max(0, size - unfilled)
    avg = o.get("average_fill_price")
    return {"id": o.get("id"), "client_order_id": o.get("client_order_id"),
            "state": o.get("state"), "filled": filled,
            "price": float(avg) if avg not in (None, "", "0") and filled else None,
            "fee": float(o.get("paid_commission") or 0)}
