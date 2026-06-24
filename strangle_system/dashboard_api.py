"""
strangle_system/dashboard_api.py
================================
Serves all layer outputs + data-collection status to the existing paper-trade
web app (deployment/) as the "Strangle System" tab.

Every yes/no is explainable from what's on screen (build spec §6): the verdict,
its reason, the L1 VRP/IV-rank, L2 trend/expiry guardrails, and L3 term/skew —
plus a data-collection panel showing forward-accumulation progress (the system
is paper-log only until the VRP-validation backtest can run).

get_status() is cached briefly so dashboard polling doesn't re-fit GARCH on
every request.
"""

import json
import sys
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from strangle_system import config
from strangle_system.data.chain_loader import ChainLoader
from strangle_system.decision_runner import decide
from strangle_system.layers.l4_gex import compute_l4, per_strike_gex
from strangle_system.backtest.vrp_validation import MIN_USABLE_ROWS

IST = timezone(timedelta(hours=5, minutes=30))

_CACHE = {"ts": 0.0, "payload": None}
_CACHE_TTL = 60.0   # seconds


def _manifest() -> dict:
    f = config.CHAIN_MANIFEST_FILE
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_updated": None, "snapshots": {}}


def _data_collection() -> dict:
    """Forward-accumulation status for the data-collection panel."""
    man = _manifest()
    loader = ChainLoader()
    latest_per = []
    total_days = set()
    for u in config.ACTIVE_UNDERLYINGS:
        dates = loader.available_dates(u)
        total_days.update(str(d) for d in dates)
        key = f"{u}_{dates[-1]}" if dates else None
        meta = man.get("snapshots", {}).get(key, {}) if key else {}
        latest_per.append({
            "underlying": u,
            "snapshot_days": len(dates),
            "latest_date": str(dates[-1]) if dates else None,
            "rows": meta.get("rows"),
            "expiries": meta.get("expiries"),
            "spot": meta.get("spot"),
            "greeks_present": meta.get("greeks_present"),
            "drive_uploaded": meta.get("drive_uploaded"),
            "captured_at": meta.get("captured_at"),
        })
    n_days = len(total_days)
    return {
        "last_updated": man.get("last_updated"),
        "per_underlying": latest_per,
        "distinct_days": n_days,
        "validation_needed": MIN_USABLE_ROWS,
        "validation_progress_pct": round(min(100.0, 100.0 * n_days / MIN_USABLE_ROWS), 1),
        "validation_status": ("insufficient_history" if n_days < MIN_USABLE_ROWS else "ready"),
    }


def _gex_block(u: str, asof: date, loader: ChainLoader) -> dict:
    """GEX levels + a trimmed per-strike profile (ATM ± 15 strikes) for the chart."""
    sig = compute_l4(u, asof, loader)
    snap = loader.snapshot_asof(u, asof)
    profile = []
    if snap is not None and not snap.empty:
        from strangle_system.data.chain_loader import ChainLoader as CL
        prof = per_strike_gex(snap, u, CL.nearest_expiry(snap))
        spot = CL.spot(snap)
        if not prof.empty and spot is not None:
            prof = prof.iloc[(prof["strike"] - spot).abs().argsort()[:31]].sort_values("strike")
            profile = [{"strike": float(r.strike), "net_gex": float(r.net_gex)} for r in prof.itertuples()]
    return {
        "net_gex": sig.net_gex, "gamma_flip": sig.gamma_flip,
        "call_wall": sig.call_wall, "put_wall": sig.put_wall,
        "regime": sig.gex_regime.value if sig.gex_regime else None,
        "validated": sig.validated, "profile": profile,
    }


def _underlying_block(u: str, asof: date, loader: ChainLoader) -> dict:
    dec, raw = decide(u, asof, loader)
    l1, l2, l3 = raw["l1"], raw["l2"], raw["l3"]
    return {
        "underlying": u,
        "trade": dec.trade,
        "verdict_reason": dec.verdict_reason,
        "l1": {k: l1.get(k) for k in
               ("atm_iv", "rv_5", "rv_10", "rv_20", "rv_forecast", "rv_forecast_method",
                "vrp", "iv_rank", "iv_percentile", "horizon_days", "data_quality")},
        "l2": {"trend_regime": l2.get("trend_regime"), "adx": l2.get("adx"),
               "is_expiry_day": l2.get("is_expiry_day"), "days_to_expiry": l2.get("days_to_expiry"),
               "event_veto": l2.get("event_veto"), "event_reason": l2.get("event_reason")},
        "l3": {"term_state": l3.get("term_state"), "term_structure_slope": l3.get("term_structure_slope"),
               "skew_state": l3.get("skew_state"), "skew_25d": l3.get("skew_25d")},
        "gex": _gex_block(u, asof, loader),
    }


def get_status(force: bool = False) -> dict:
    """Full strangle-system status for the dashboard (cached ~60s)."""
    now = time.time()
    if not force and _CACHE["payload"] is not None and (now - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["payload"]

    asof = datetime.now(IST).date()
    loader = ChainLoader()
    underlyings = []
    for u in config.ACTIVE_UNDERLYINGS:
        try:
            underlyings.append(_underlying_block(u, asof, loader))
        except Exception as exc:
            underlyings.append({"underlying": u, "trade": False,
                                "verdict_reason": f"error: {exc}", "l1": {}, "l2": {}, "l3": {}})

    payload = {
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "phase": "v1 (L1 VRP + L2 guardrails); L3 informational. Paper-log only.",
        "underlyings": underlyings,
        "data_collection": _data_collection(),
    }
    _CACHE.update(ts=now, payload=payload)
    return payload


if __name__ == "__main__":
    config.reconfigure_stdout()
    print(json.dumps(get_status(force=True), indent=2, default=str))
