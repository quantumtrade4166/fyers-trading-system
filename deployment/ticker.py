"""
ticker.py
Live ticker-tape data for the Terminal tab — Indian indices + large-cap stocks.
Single batched Fyers Quotes call (LTP + change%), cached ~5s. Read-only: never
generates a token (just reads config/access_token.txt), so it's safe alongside
the VPS feed.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import time
import threading
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

ACCESS_TOKEN_PATH = Path(os.getenv("ACCESS_TOKEN_PATH", r"G:\fyers_data_pipeline\config\access_token.txt"))
APP_ID            = os.getenv("FYERS_APP_ID", "W09OMXQB8J-100")

# (fyers symbol, display label) — indices first, then large caps. Order is preserved.
SYMBOLS = [
    ("NSE:NIFTY50-INDEX",    "NIFTY 50"),
    ("BSE:SENSEX-INDEX",     "SENSEX"),
    ("NSE:NIFTYBANK-INDEX",  "BANKNIFTY"),
    ("NSE:FINNIFTY-INDEX",   "FINNIFTY"),
    ("NSE:MIDCPNIFTY-INDEX", "MIDCPNIFTY"),
    ("NSE:INDIAVIX-INDEX",   "INDIA VIX"),
    ("NSE:RELIANCE-EQ",      "RELIANCE"),
    ("NSE:HDFCBANK-EQ",      "HDFCBANK"),
    ("NSE:ICICIBANK-EQ",     "ICICIBANK"),
    ("NSE:TCS-EQ",           "TCS"),
    ("NSE:INFY-EQ",          "INFY"),
    ("NSE:SBIN-EQ",          "SBIN"),
    ("NSE:BHARTIARTL-EQ",    "BHARTIARTL"),
    ("NSE:ITC-EQ",           "ITC"),
    ("NSE:LT-EQ",            "LT"),
    ("NSE:KOTAKBANK-EQ",     "KOTAKBANK"),
    ("NSE:AXISBANK-EQ",      "AXISBANK"),
    ("NSE:BAJFINANCE-EQ",    "BAJFINANCE"),
    ("NSE:MARUTI-EQ",        "MARUTI"),
    ("NSE:HINDUNILVR-EQ",    "HINDUNILVR"),
    ("NSE:NTPC-EQ",          "NTPC"),
    ("NSE:TITAN-EQ",         "TITAN"),
]

_CACHE_TTL = 3.0          # seconds — near-tick refresh
_lock  = threading.Lock()
_cache = {"ts": 0.0, "data": []}
_inflight = False


def _get_fyers():
    raw = ACCESS_TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError("Empty access token")
    try:
        import json as _json
        token = _json.loads(raw)["token"]
    except Exception:
        token = raw
    from fyers_apiv3 import fyersModel
    return fyersModel.FyersModel(
        client_id=f"{APP_ID}:{token}",
        is_async=False,
        token=token,
        log_path="",
    )


def _fetch():
    fyers = _get_fyers()
    label_by_sym = {s: lbl for s, lbl in SYMBOLS}
    syms_str = ",".join(s for s, _ in SYMBOLS)
    resp = fyers.quotes({"symbols": syms_str})
    if resp.get("s") != "ok":
        raise RuntimeError(resp.get("message", "quotes error"))
    by_sym = {}
    for item in resp.get("d", []):
        sym = item.get("n", "")
        v   = item.get("v", {}) or {}
        lp  = v.get("lp")
        if sym in label_by_sym and lp is not None:
            by_sym[sym] = {
                "name": label_by_sym[sym],
                "ltp":  float(lp),
                "ch":   float(v.get("ch")  or 0.0),
                "chp":  float(v.get("chp") or 0.0),
            }
    # preserve SYMBOLS order
    return [by_sym[s] for s, _ in SYMBOLS if s in by_sym]


def get_ticker(force: bool = False):
    """Return list of {name, ltp, ch, chp}. Cached; serves stale cache on error.

    The lock is NOT held during the network fetch, and a single in-flight guard
    ensures overlapping polls serve cached data instead of each launching a fetch
    (which would block thread-pool workers and starve other endpoints).
    """
    global _inflight
    now = time.time()
    with _lock:
        if not force and _cache["data"] and (now - _cache["ts"] < _CACHE_TTL):
            return _cache["data"]
        if _inflight and _cache["data"]:
            return _cache["data"]
        _inflight = True
    try:
        data = _fetch()
        with _lock:
            _cache["data"] = data
            _cache["ts"]   = time.time()
    except Exception as e:
        print(f"  [ticker] fetch error: {e}")
    finally:
        with _lock:
            _inflight = False
    with _lock:
        return _cache["data"]
