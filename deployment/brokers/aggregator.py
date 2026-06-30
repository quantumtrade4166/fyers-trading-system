"""
aggregator.py
Fetches every broker snapshot in parallel and merges into one combined view
for the Terminal tab. Short TTL cache so frontend polling never hammers the
broker APIs.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import time
import threading
from concurrent.futures import ThreadPoolExecutor

from deployment.brokers.zerodha_adapter import ZerodhaAdapter
# XTS / Jainam intentionally DISABLED — no XTS API calls until re-enabled.
# from deployment.brokers.jainam_adapter import JainamAdapter

# Fyers intentionally excluded — not used for placing orders.
# XTS (JainamAdapter) removed from the active list to stop all XTS API activity.
ADAPTERS = [ZerodhaAdapter()]

_CACHE_TTL = 3.0          # seconds
_lock = threading.Lock()
_cache = {"ts": 0.0, "data": None}


def _fetch_all() -> dict:
    with ThreadPoolExecutor(max_workers=len(ADAPTERS)) as ex:
        snaps = list(ex.map(lambda a: a.fetch_snapshot(), ADAPTERS))

    brokers = [s.as_dict() for s in snaps]

    # day P&L = everything (open unrealised + intraday booked).
    combined_pnl   = round(sum(s.total_pnl for s in snaps), 2)
    open_positions = sum(s.open_count for s in snaps)
    connected      = sum(1 for s in snaps if s.status == "ok")

    # booked/realised today = P&L on legs already squared off (net qty 0)
    combined_realised = round(
        sum(p["pnl"] for s in brokers for p in s["positions"] if p["qty"] == 0), 2)

    # table shows only OPEN positions (qty != 0) — closed legs are noise on a live board.
    # MCX positions always sink to the bottom; within each group, largest |P&L| first.
    all_positions = [p for s in brokers for p in s["positions"] if p["qty"] != 0]
    all_positions.sort(key=lambda p: ("MCX" in str(p.get("exchange", "")).upper(), -abs(p["pnl"])))

    return {
        "combined_pnl":      combined_pnl,
        "combined_realised": combined_realised,
        "open_positions":    open_positions,
        "brokers_connected": connected,
        "brokers_total":     len(ADAPTERS),
        "brokers":           brokers,
        "positions":         all_positions,
        "ts":                time.strftime("%H:%M:%S"),
    }


def get_terminal(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        if not force and _cache["data"] and (now - _cache["ts"] < _CACHE_TTL):
            return _cache["data"]
    data = _fetch_all()
    with _lock:
        _cache["data"] = data
        _cache["ts"]   = now
    return data
