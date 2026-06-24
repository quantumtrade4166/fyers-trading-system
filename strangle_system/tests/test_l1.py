"""
Unit tests for Layer 1 (volatility edge). Synthetic data — no token/network.
Validates estimator math, ATM-IV interpolation, IV-rank guard, and VRP arithmetic.
"""

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from strangle_system.layers import l1_volatility as l1
from strangle_system.data.chain_collector import SNAPSHOT_COLUMNS


def _ohlc_from_closes(closes):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    c = pd.Series(closes, index=idx, dtype=float)
    # tight bars so GK/YZ ~ close-to-close; open = prev close (no gap)
    o = c.shift(1).fillna(c.iloc[0])
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.001,
                         "low": np.minimum(o, c) * 0.999, "close": c})


def test_close_to_close_matches_manual():
    rng = np.random.default_rng(0)
    rets = rng.normal(0, 0.01, 260)
    closes = 100 * np.exp(np.cumsum(rets))
    ohlc = _ohlc_from_closes(closes)
    got = l1.rv_close_to_close(ohlc, 20)
    r = np.log(ohlc["close"] / ohlc["close"].shift(1)).dropna()
    expect = r.tail(20).std(ddof=1) * np.sqrt(252)
    assert abs(got - expect) < 1e-9


def test_estimators_positive_and_finite():
    rng = np.random.default_rng(1)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, 120)))
    ohlc = _ohlc_from_closes(closes)
    for fn in (l1.rv_close_to_close, l1.rv_garman_klass, l1.rv_yang_zhang):
        v = fn(ohlc, 20)
        assert v is not None and v > 0 and np.isfinite(v)


def test_low_vol_series_low_rv():
    closes = 100 * np.exp(np.cumsum(np.full(120, 0.0001)))  # near-constant drift
    ohlc = _ohlc_from_closes(closes)
    assert l1.rv_close_to_close(ohlc, 20) < 0.01


def test_insufficient_window_returns_none():
    ohlc = _ohlc_from_closes(100 + np.arange(10))
    assert l1.rv_close_to_close(ohlc, 20) is None
    assert l1.forecast_garch(ohlc) is None       # <50 points


def test_ewma_forecast_positive():
    rng = np.random.default_rng(2)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    f = l1.forecast_ewma(_ohlc_from_closes(closes))
    assert f is not None and f > 0


def _snapshot(strikes_iv, spot, expiry="2026-06-30"):
    rows = []
    for strike, iv in strikes_iv:
        for ot in ("CE", "PE"):
            rows.append({"date": "2026-06-24", "underlying": "NIFTY", "expiry": expiry,
                         "strike": float(strike), "opt_type": ot, "spot": spot,
                         "iv": iv})
    df = pd.DataFrame(rows)
    return df.reindex(columns=SNAPSHOT_COLUMNS)


def test_atm_iv_interpolation():
    # strikes 100/110, iv 10/20 (%), spot 105 → ATM IV 15% → 0.15 decimal
    snap = _snapshot([(100, 10.0), (110, 20.0)], spot=105.0)
    iv = l1.atm_iv_from_snapshot(snap)
    assert abs(iv - 0.15) < 1e-9


def test_atm_iv_at_exact_strike():
    snap = _snapshot([(100, 12.0), (110, 18.0)], spot=100.0)
    assert abs(l1.atm_iv_from_snapshot(snap) - 0.12) < 1e-9


def test_iv_rank_insufficient_history():
    s = pd.Series([0.12, 0.13, 0.14])
    rank, pct = l1.iv_rank_percentile(s, 0.13)
    assert rank is None and pct is None


def test_iv_rank_full():
    s = pd.Series(np.linspace(0.10, 0.20, 30))
    rank, pct = l1.iv_rank_percentile(s, 0.15)
    assert rank is not None and 45 <= rank <= 55      # mid-range
    assert 40 <= pct <= 60


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
