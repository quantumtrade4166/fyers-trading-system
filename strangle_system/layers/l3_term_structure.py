"""
strangle_system/layers/l3_term_structure.py
============================================
LAYER 3 — term structure & skew. Refines strike selection and the calm/stress
read; feeds the eventual L5 score (does not gate the v1 flag).

  - Term structure: ATM IV across consecutive expiries.
      slope = far_ATM_IV − near_ATM_IV   (annualized decimal vol points)
      CONTANGO (near < far) = calm, supportive of selling;
      BACKWARDATION (near > far) = stress, caution.
  - Skew: 25-delta put IV − 25-delta call IV for the nearest expiry, using the
      real per-strike delta + IV from the chain (greeks=1). Positive = puts
      richer (typical index put skew); informs symmetric vs skewed strangle.

Point-in-time: uses only the snapshot at/before asof. Fail-safe: missing legs
→ None + DEGRADED, never a fabricated state.
"""

import sys
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from strangle_system import config
from strangle_system.signals import TermStructureSignal, TermState, DataQuality
from strangle_system.data.chain_loader import ChainLoader
from strangle_system.layers.l1_volatility import atm_iv_from_snapshot


def _interp_iv_at_delta(rows: pd.DataFrame, target_delta: float) -> Optional[float]:
    """
    Interpolate IV (decimal) at a target signed delta among one option side.
    rows: CE rows (delta>0) or PE rows (delta<0) with 'delta' and 'iv'.
    """
    r = rows.dropna(subset=["delta", "iv"]).copy()
    if len(r) < 2:
        return None
    r = r.sort_values("delta")
    x = r["delta"].values.astype(float)
    y = (r["iv"].values.astype(float)) / 100.0     # Fyers IV % → decimal
    if target_delta < x.min() or target_delta > x.max():
        # outside coverage → np.interp would clamp; flag by returning clamped value
        return float(np.interp(target_delta, x, y))
    return float(np.interp(target_delta, x, y))


def term_structure(snapshot: pd.DataFrame) -> tuple[Optional[float], TermState]:
    """far_ATM_IV − near_ATM_IV across the two nearest expiries."""
    expiries = sorted(e for e in snapshot["expiry"].dropna().unique())
    if len(expiries) < 2:
        return None, TermState.FLAT
    near_iv = atm_iv_from_snapshot(snapshot, expiries[0])
    far_iv = atm_iv_from_snapshot(snapshot, expiries[1])
    if near_iv is None or far_iv is None:
        return None, TermState.FLAT
    slope = far_iv - near_iv
    if slope > config.TERM_STATE_THRESHOLD:
        return round(slope, 4), TermState.CONTANGO
    if slope < -config.TERM_STATE_THRESHOLD:
        return round(slope, 4), TermState.BACKWARDATION
    return round(slope, 4), TermState.FLAT


def skew_25d(snapshot: pd.DataFrame, expiry: Optional[str] = None) -> tuple[Optional[float], str]:
    """25-delta put IV − 25-delta call IV for one expiry (default nearest)."""
    sl = ChainLoader.expiry_slice(snapshot, expiry)
    if sl.empty:
        return None, ""
    d = config.SKEW_DELTA
    call_iv = _interp_iv_at_delta(sl[sl["opt_type"] == "CE"], d)
    put_iv = _interp_iv_at_delta(sl[sl["opt_type"] == "PE"], -d)
    if call_iv is None or put_iv is None:
        return None, ""
    sk = put_iv - call_iv
    if sk > config.SKEW_STATE_THRESHOLD:
        state = "PUT_SKEW"
    elif sk < -config.SKEW_STATE_THRESHOLD:
        state = "CALL_SKEW"
    else:
        state = "FLAT"
    return round(sk, 4), state


def compute_l3(underlying: str, asof: Optional[date] = None,
               loader: Optional[ChainLoader] = None) -> TermStructureSignal:
    asof = asof or date.today()
    loader = loader or ChainLoader()
    sig = TermStructureSignal(underlying=underlying, asof=str(asof))

    snapshot = loader.snapshot_asof(underlying, asof)
    if snapshot is None or snapshot.empty:
        sig.data_quality = DataQuality.MISSING
        return sig

    sig.term_structure_slope, sig.term_state = term_structure(snapshot)
    sig.skew_25d, sig.skew_state = skew_25d(snapshot)

    if sig.term_structure_slope is None or sig.skew_25d is None:
        sig.data_quality = DataQuality.DEGRADED
    return sig


if __name__ == "__main__":
    config.reconfigure_stdout()
    for u in config.ACTIVE_UNDERLYINGS:
        s = compute_l3(u)
        print(f"\n=== L3 / {u} ({s.asof}) ===")
        print(f"  term_slope : {s.term_structure_slope}  state: {s.term_state.value}")
        print(f"  skew_25d   : {s.skew_25d}  state: {s.skew_state}")
        print(f"  data_quality: {s.data_quality.value}")
