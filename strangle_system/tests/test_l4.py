"""Unit tests for Layer 4 GEX. Synthetic snapshots. GEX stays gated (validated=False)."""

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from strangle_system.layers import l4_gex as l4
from strangle_system.signals import GexRegime
from strangle_system.data.chain_collector import SNAPSHOT_COLUMNS
from strangle_system import config


def _snap(rows, spot):
    out = []
    for (strike, ot, gamma, oi) in rows:
        out.append({"expiry": "2026-06-30", "opt_type": ot, "strike": float(strike),
                    "spot": spot, "gamma": gamma, "oi": oi})
    return pd.DataFrame(out).reindex(columns=SNAPSHOT_COLUMNS)


def test_per_strike_sign_convention():
    snap = _snap([(100, "CE", 0.01, 1000), (100, "PE", 0.01, 2000)], spot=100.0)
    prof = l4.per_strike_gex(snap, "NIFTY")
    row = prof.iloc[0]
    assert row["call_gex"] > 0          # calls positive
    assert row["put_gex"] < 0           # puts negative
    # magnitudes scale with OI: put OI 2× call OI
    assert abs(row["put_gex"]) == 2 * row["call_gex"]


def test_walls_and_regime():
    # put-heavy low strikes, call-heavy high strikes
    rows = [
        (95, "PE", 0.02, 5000), (95, "CE", 0.005, 500),
        (100, "PE", 0.01, 1000), (100, "CE", 0.01, 1000),
        (105, "CE", 0.02, 6000), (105, "PE", 0.005, 400),
    ]
    snap = _snap(rows, spot=100.0)
    sig = l4.compute_l4("NIFTY", loader=_FakeLoader(snap))
    assert sig.call_wall == 105.0       # biggest call gamma×OI
    assert sig.put_wall == 95.0         # biggest put gamma×OI
    assert sig.gex_regime in (GexRegime.POSITIVE, GexRegime.NEGATIVE)
    assert sig.validated is False       # GATE: never on until backtest


def test_gamma_flip_interpolated():
    # cumulative net crosses zero between strikes
    prof = pd.DataFrame({"strike": [95, 100, 105], "net_gex": [-100.0, -50.0, 200.0]})
    flip = l4._gamma_flip(prof)
    # cumsum: -100, -150, 50 → crosses between 100 and 105
    assert 100.0 < flip <= 105.0


def test_gate_respected():
    assert config.GEX_VALIDATED is False    # must remain gated until validation


class _FakeLoader:
    """Minimal loader returning a fixed snapshot for compute_l4."""
    def __init__(self, snap): self._snap = snap
    def snapshot_asof(self, u, a): return self._snap


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
