"""Unit tests for Layer 3 (term structure & skew). Synthetic snapshots."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from strangle_system.layers import l3_term_structure as l3
from strangle_system.signals import TermState
from strangle_system.data.chain_collector import SNAPSHOT_COLUMNS


def _snap(rows):
    return pd.DataFrame(rows).reindex(columns=SNAPSHOT_COLUMNS)


def _expiry_strikes(expiry, spot, iv_pct, strikes=(95, 100, 105)):
    """Flat-IV strikes for one expiry (so ATM IV ≈ iv_pct)."""
    out = []
    for k in strikes:
        for ot in ("CE", "PE"):
            out.append({"expiry": expiry, "opt_type": ot, "strike": float(k),
                        "spot": spot, "iv": iv_pct, "delta": 0.5 if ot == "CE" else -0.5})
    return out


def test_term_structure_contango():
    rows = _expiry_strikes("2026-06-30", 100.0, 10.0) + _expiry_strikes("2026-07-07", 100.0, 12.0)
    slope, state = l3.term_structure(_snap(rows))
    assert state == TermState.CONTANGO and abs(slope - 0.02) < 1e-6


def test_term_structure_backwardation():
    rows = _expiry_strikes("2026-06-30", 100.0, 15.0) + _expiry_strikes("2026-07-07", 100.0, 11.0)
    slope, state = l3.term_structure(_snap(rows))
    assert state == TermState.BACKWARDATION and slope < 0


def test_term_structure_single_expiry_flat():
    rows = _expiry_strikes("2026-06-30", 100.0, 12.0)
    slope, state = l3.term_structure(_snap(rows))
    assert slope is None and state == TermState.FLAT


def test_skew_put_richer():
    # nearest expiry: build CE (delta 0.1..0.6) and PE (delta -0.1..-0.6).
    rows = []
    for delta, iv in [(0.10, 11.0), (0.25, 12.0), (0.50, 13.0), (0.60, 13.5)]:
        rows.append({"expiry": "2026-06-30", "opt_type": "CE", "strike": 100 + delta * 100,
                     "spot": 100.0, "iv": iv, "delta": delta})
    for delta, iv in [(-0.10, 14.0), (-0.25, 15.0), (-0.50, 16.0), (-0.60, 16.5)]:
        rows.append({"expiry": "2026-06-30", "opt_type": "PE", "strike": 100 + delta * 100,
                     "spot": 100.0, "iv": iv, "delta": delta})
    sk, state = l3.skew_25d(_snap(rows))
    # put 25d IV 15% − call 25d IV 12% = 3 vol pts = 0.03
    assert abs(sk - 0.03) < 1e-6 and state == "PUT_SKEW"


def test_skew_interp_at_delta():
    rows = pd.DataFrame([
        {"opt_type": "CE", "delta": 0.10, "iv": 10.0},
        {"opt_type": "CE", "delta": 0.40, "iv": 16.0},
    ])
    # at delta 0.25 → midpoint → 13% → 0.13
    iv = l3._interp_iv_at_delta(rows, 0.25)
    assert abs(iv - 0.13) < 1e-9


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
