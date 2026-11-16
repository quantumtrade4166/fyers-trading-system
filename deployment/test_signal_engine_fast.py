"""deployment/test_signal_engine_fast.py — the closed-form regression of 16-Nov-2026 gives the SAME signals
as the statsmodels one it replaced.

`_ref_beta` / `_ref_half_life` below are the old helpers, word for word. Every test runs the engine both
ways and compares the full answers (z, beta, spread, half-life, 60-point history, signal, hl_ok)."""
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
import pytest
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

from deployment import signal_engine as S
from deployment.pair_config import PAIRS, NAME, SYM_A, SYM_B, LOOKBACK


def _ref_beta(pa, pb):
    try:
        return OLS(pa, add_constant(pb)).fit().params[1]
    except Exception:
        return np.nan


def _ref_half_life(spread):
    try:
        phi = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
        return -np.log(2) / np.log(1 + phi) if phi < 0 else 999.0
    except Exception:
        return 999.0


def _both(monkeypatch, fn):
    """fn() with the new maths, then with the old maths."""
    new = fn()
    with monkeypatch.context() as m:
        m.setattr(S, "_ols_beta", _ref_beta)
        m.setattr(S, "_half_life", _ref_half_life)
        old = fn()
    return new, old


def _same(a, b, tol=1e-9):
    """Equal, allowing only float noise far below the 4 decimals the engine reports."""
    if isinstance(a, dict):
        assert a.keys() == b.keys()
        return all(_same(a[k], b[k], tol) for k in a)
    if isinstance(a, (list, tuple)):
        assert len(a) == len(b)
        return all(_same(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, float):
        if np.isnan(a) or np.isnan(b):
            assert np.isnan(a) and np.isnan(b)
            return True
        assert abs(a - b) <= tol * max(1.0, abs(a), abs(b)), (a, b)
        return True
    assert a == b, (a, b)
    return True


def test_slope_matches_statsmodels_on_random_data():
    rnd = np.random.default_rng(7)
    for n in (10, 11, 30, 60, 250, 5000):
        for _ in range(40):
            x = np.cumsum(rnd.normal(0, 1, n)) + rnd.uniform(50, 5000)
            y = 0.7 * x + np.cumsum(rnd.normal(0, 1, n)) + rnd.uniform(-200, 200)
            assert abs(S._ols_beta(y, x) - _ref_beta(y, x)) <= 1e-9 * max(1.0, abs(_ref_beta(y, x)))
            sp = y - 0.7 * x
            # (both give "not a number" when the spread overshoots, phi < -1: unchanged old behaviour)
            assert _same(float(S._half_life(sp)), float(_ref_half_life(sp)), tol=1e-7)


def test_no_line_to_fit_gives_the_old_answers():
    flat = np.full(30, 812.5)
    y = np.linspace(1, 2, 30)
    assert np.isnan(S._ols_beta(y, flat)) and np.isnan(_ref_beta(y, flat))          # x never moves
    assert S._half_life(flat) == _ref_half_life(flat) == 999.0                       # spread never moves
    bad = y.copy()
    bad[5] = np.nan
    assert np.isnan(S._ols_beta(y, bad)) and np.isnan(_ref_beta(y, bad))
    assert S._half_life(bad) == _ref_half_life(bad) == 999.0
    up = np.arange(40.0) ** 1.5                                                       # trending: no reversion
    assert S._half_life(up) == _ref_half_life(up) == 999.0


def _fake_pair(n=700, seed=3):
    rnd = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-02", periods=n)
    b = pd.Series(1500 + np.cumsum(rnd.normal(0, 12, n)), index=idx, name="BBB")
    a = pd.Series(0.8 * b.values + 300 + np.cumsum(rnd.normal(0, 3, n)) * 0.4 + rnd.normal(0, 9, n), index=idx, name="AAA")
    return a, b


@pytest.mark.parametrize("lookback", [20, 30, 45, 60, 90, 120])
def test_pair_zscore_same_both_ways_on_made_up_prices(monkeypatch, lookback):
    a, b = _fake_pair()
    for cut in range(lookback + 5, len(a), 37):                      # many different "todays"
        new, old = _both(monkeypatch, lambda: S.compute_pair_zscore(a.iloc[:cut], b.iloc[:cut], lookback))
        assert _same(new, old)
    live = {"AAA": float(a.iloc[-1]) * 1.013, "BBB": float(b.iloc[-1]) * 0.991}
    new, old = _both(monkeypatch, lambda: S.compute_pair_zscore(a, b, lookback, live))
    assert _same(new, old)


def _real_prices():
    try:
        return S.load_prices()
    except Exception:
        return {}


def test_every_real_pair_same_both_ways_over_the_last_300_days(monkeypatch):
    """The real pairs on the real price files, each of the last 300 trading days treated as 'today'."""
    prices = _real_prices()
    pairs = [p for p in PAIRS if p[SYM_A] in prices and p[SYM_B] in prices]
    if not pairs:
        pytest.skip("price files not on this machine")
    checked = flips = 0
    for p in pairs:
        sa, sb = prices[p[SYM_A]].rename(p[SYM_A]), prices[p[SYM_B]].rename(p[SYM_B])
        n = min(len(sa), len(sb))
        for back in range(0, 300, 3):
            a, b = (sa.iloc[:len(sa) - back], sb.iloc[:len(sb) - back]) if back else (sa, sb)
            new, old = _both(monkeypatch, lambda: S.compute_pair_zscore(a, b, p[LOOKBACK]))
            assert _same(new, old), (p[NAME], back)
            checked += 1
            flips += new != old                                       # a 4th-decimal rounding flip
        new, old = _both(monkeypatch, lambda: S.compute_max_hl(sa.values[-n:], sb.values[-n:]))
        assert abs(new - old) <= 1e-6 * max(1.0, abs(old)), p[NAME]
    print(f"\n  real pairs: {len(pairs)}, days checked: {checked}, last-decimal rounding flips: {flips}")


def test_all_signals_same_both_ways_on_the_real_files(monkeypatch):
    prices = _real_prices()
    if not prices:
        pytest.skip("price files not on this machine")
    monkeypatch.setattr(S, "_prices_cache", prices)
    monkeypatch.setattr(S, "_today_override", {})
    monkeypatch.setattr(S, "_max_hl_cache", {})
    new, old = _both(monkeypatch, S.get_all_signals)
    assert _same(new, old)
    assert {k: v.get("signal") for k, v in new.items()} == {k: v.get("signal") for k, v in old.items()}
    assert {k: v.get("hl_ok") for k, v in new.items()} == {k: v.get("hl_ok") for k, v in old.items()}
    live = {s: float(v.iloc[-1]) * (1.02 if i % 2 else 0.985) for i, (s, v) in enumerate(prices.items())}
    new, old = _both(monkeypatch, lambda: S.get_all_signals(live))
    assert _same(new, old)
    assert {k: v.get("signal") for k, v in new.items()} == {k: v.get("signal") for k, v in old.items()}
