"""Unit tests for Layer 2 guardrails + v1 decision gate logic. Synthetic data."""

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from strangle_system.layers import l2_guardrails as l2
from strangle_system.signals import TrendRegime, VolatilitySignal, GuardrailSignal, DataQuality


def _ohlc(closes, rng_frac=0.002):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    c = pd.Series(closes, index=idx, dtype=float)
    o = c.shift(1).fillna(c.iloc[0])
    hi = np.maximum(o, c) * (1 + rng_frac)
    lo = np.minimum(o, c) * (1 - rng_frac)
    return pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c})


def test_adx_higher_in_trend_than_range():
    trend = _ohlc(100 * np.exp(np.cumsum(np.full(120, 0.01))))      # steady uptrend
    rng = _ohlc(100 + np.sin(np.linspace(0, 30, 120)) * 2)          # oscillating
    a_trend, a_range = l2.adx(trend), l2.adx(rng)
    assert a_trend is not None and a_range is not None
    assert a_trend > a_range
    assert a_trend > l2.config.ADX_TREND_MIN          # strong trend registers


def test_ema_alignment():
    assert l2.ema_aligned(_ohlc(100 * np.exp(np.cumsum(np.full(80, 0.01))))) is True
    # flat series: close == ema20 == ema50 → strict ordering fails → not aligned
    assert l2.ema_aligned(_ohlc(np.full(80, 100.0))) is False


def test_trend_regime_classes():
    trend = _ohlc(100 * np.exp(np.cumsum(np.full(120, 0.012))))
    reg, a = l2.trend_regime(trend)
    assert reg == TrendRegime.STRONG_TREND
    flat = _ohlc(100 + np.sin(np.linspace(0, 40, 120)) * 0.5)
    reg2, _ = l2.trend_regime(flat)
    assert reg2 in (TrendRegime.RANGE, TrendRegime.WEAK_TREND)


def test_event_calendar_veto():
    cal = pd.DataFrame({"date": [date(2026, 8, 6)], "event": ["RBI MPC"],
                        "severity": ["high"]})
    veto, sev, reason = l2.event_for(date(2026, 8, 6), cal)
    assert veto is True and sev == "high" and "RBI" in reason
    veto2, _, _ = l2.event_for(date(2026, 8, 7), cal)
    assert veto2 is False


def test_event_medium_not_veto():
    cal = pd.DataFrame({"date": [date(2026, 7, 15)], "event": ["CPI"],
                        "severity": ["medium"]})
    veto, sev, _ = l2.event_for(date(2026, 7, 15), cal)
    assert veto is False and sev == "medium"


def test_next_weekday_expiry():
    # 2026-06-24 is a Wednesday(2). NIFTY weekday=Tuesday(1) → next Tue = 2026-06-30
    assert l2._next_weekday(date(2026, 6, 24), 1) == date(2026, 6, 30)
    # Thursday(3) from Wed → next day 2026-06-25
    assert l2._next_weekday(date(2026, 6, 24), 3) == date(2026, 6, 25)


def test_decision_gates(monkeypatch):
    from strangle_system import decision_runner as dr

    def mk(vrp, trend, expiry=False, event=False, dq=DataQuality.OK):
        l1 = VolatilitySignal(underlying="NIFTY", asof="2026-06-24",
                              atm_iv=0.15, rv_forecast=0.15 - (vrp or 0),
                              vrp=vrp, data_quality=dq)
        g = GuardrailSignal(underlying="NIFTY", asof="2026-06-24",
                            event_veto=event, is_expiry_day=expiry,
                            days_to_expiry=0 if expiry else 5, trend_regime=trend)
        return l1, g

    # rich VRP, calm range → TRADE
    monkeypatch.setattr(dr, "compute_l1", lambda u, a, l: mk(0.05, TrendRegime.RANGE)[0])
    monkeypatch.setattr(dr, "compute_l2", lambda u, a, l: mk(0.05, TrendRegime.RANGE)[1])
    dec, _ = dr.decide("NIFTY", date(2026, 6, 24))
    assert dec.trade is True

    # thin VRP → NO-TRADE
    monkeypatch.setattr(dr, "compute_l1", lambda u, a, l: mk(0.005, TrendRegime.RANGE)[0])
    monkeypatch.setattr(dr, "compute_l2", lambda u, a, l: mk(0.005, TrendRegime.RANGE)[1])
    assert dr.decide("NIFTY", date(2026, 6, 24))[0].trade is False

    # rich VRP but expiry day → NO-TRADE
    monkeypatch.setattr(dr, "compute_l1", lambda u, a, l: mk(0.05, TrendRegime.RANGE, expiry=True)[0])
    monkeypatch.setattr(dr, "compute_l2", lambda u, a, l: mk(0.05, TrendRegime.RANGE, expiry=True)[1])
    assert dr.decide("NIFTY", date(2026, 6, 24))[0].trade is False

    # rich VRP but strong trend → NO-TRADE
    monkeypatch.setattr(dr, "compute_l1", lambda u, a, l: mk(0.05, TrendRegime.STRONG_TREND)[0])
    monkeypatch.setattr(dr, "compute_l2", lambda u, a, l: mk(0.05, TrendRegime.STRONG_TREND)[1])
    assert dr.decide("NIFTY", date(2026, 6, 24))[0].trade is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
