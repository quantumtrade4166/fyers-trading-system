"""
aggregator.py
Fetches every broker snapshot in parallel and merges into one combined view
for the Terminal tab. Short TTL cache so frontend polling never hammers the
broker APIs.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import time
import threading
from datetime import datetime, time as dtime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytz

from deployment.brokers.zerodha_adapter import ZerodhaAdapter
from deployment.brokers.jainam_adapter import JainamAdapter

# Fyers intentionally excluded — not used for placing orders.
# Zerodha is always polled. XTS/Jainam is TIME-GATED: it conflicts with the user's
# AlgoMate copy-trading (XTS allows ONE session per key), so we only touch XTS at/after
# 15:30 IST (market close) — by then AlgoMate is done for the day. A same-day force flag
# (deployment/xts_force_today.json) lets us turn it on earlier for an ad-hoc EOD video;
# the flag auto-expires next day so XTS never disturbs AlgoMate during live trading.
_ZERODHA = ZerodhaAdapter()
_JAINAM  = JainamAdapter()

IST            = pytz.timezone("Asia/Kolkata")
XTS_LIVE_AFTER = dtime(15, 30)     # IST — XTS auto-goes-live at market close
_FORCE_FILE    = Path(__file__).parent.parent / "xts_force_today.json"

_CACHE_TTL = 3.0          # seconds
_lock = threading.Lock()
_cache = {"ts": 0.0, "data": None}


def _xts_active() -> bool:
    """XTS only after 15:30 IST, or if a same-day force flag is present."""
    now = datetime.now(IST)
    if now.time() >= XTS_LIVE_AFTER:
        return True
    try:
        if _FORCE_FILE.exists():
            if json.loads(_FORCE_FILE.read_text()).get("date") == now.date().isoformat():
                return True
    except Exception:
        pass
    return False


def _active_adapters() -> list:
    adapters = [_ZERODHA]
    if _xts_active():
        adapters.append(_JAINAM)
    return adapters


def _fetch_all() -> dict:
    adapters = _active_adapters()
    with ThreadPoolExecutor(max_workers=len(adapters)) as ex:
        snaps = list(ex.map(lambda a: a.fetch_snapshot(), adapters))

    # XTS only exposes a ₹12cr dealer RMS limit, not the client's real margin. The XTS
    # book mirrors Zerodha exactly (AlgoMate copies the same strangle), so XTS "used"
    # mirrors Zerodha's real SPAN/exposure; XTS total capital is a fixed ₹40L, so
    # XTS available = ₹40L − used.
    XTS_CAPITAL = 4_000_000.0
    z = next((s for s in snaps if s.broker == "zerodha" and s.status == "ok"), None)
    j = next((s for s in snaps if s.broker == "jainam"  and s.status == "ok"), None)
    if z and j:
        j.margin_used      = z.margin_used
        j.margin_available = max(0.0, XTS_CAPITAL - z.margin_used)

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

    # combined margin across connected accounts
    m_used  = round(sum(s.margin_used for s in snaps), 2)
    m_avail = round(sum(s.margin_available for s in snaps), 2)
    m_total = round(m_used + m_avail, 2)
    m_util  = round(m_used / m_total * 100, 1) if m_total > 0 else 0.0

    return {
        "combined_pnl":      combined_pnl,
        "combined_realised": combined_realised,
        "open_positions":    open_positions,
        "brokers_connected": connected,
        "brokers_total":     len(adapters),
        "brokers":           brokers,
        "positions":         all_positions,
        "margin": {
            "used":        m_used,
            "available":   m_avail,
            "total":       m_total,
            "utilisation": m_util,
            "by_broker":   {s.broker: {"used": round(s.margin_used, 2),
                                       "available": round(s.margin_available, 2)}
                            for s in snaps},
        },
        "xts_active":        _xts_active(),
        "ts":                time.strftime("%H:%M:%S"),
    }


_inflight = False


def get_terminal(force: bool = False) -> dict:
    global _inflight
    now = time.time()
    with _lock:
        if not force and _cache["data"] and (now - _cache["ts"] < _CACHE_TTL):
            return _cache["data"]
        # another request is already fetching from the brokers — serve the last
        # known snapshot instead of launching a 2nd parallel fetch. Prevents the
        # shared thread-pool from being starved by fast frontend polling.
        if _inflight and _cache["data"]:
            return _cache["data"]
        _inflight = True
    try:
        data = _fetch_all()
        with _lock:
            _cache["data"] = data
            _cache["ts"]   = time.time()
    finally:
        with _lock:
            _inflight = False
    with _lock:
        return _cache["data"]
