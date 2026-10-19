"""
Sanity tests for the crypto volume engine (synthetic data, no downloads).
Run: .venv\\Scripts\\python.exe -m pytest backtesting/crypto_volume/test_engine.py -q
"""

import numpy as np
import pandas as pd
import pytest

from . import engine as E
from . import strategies as S


def _fake_panel(n_coins=5, n_bars=900, seed=0) -> E.Panel:
    rng = np.random.default_rng(seed)
    P = E.Panel.__new__(E.Panel)
    P.tf, P.bpy, P.bpd = "1d", 365, 1
    P.index = pd.date_range("2021-01-01", periods=n_bars, freq="1D")
    cols = [f"C{i}USDT" for i in range(n_coins)]
    px = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.03, (n_bars, n_coins)), axis=0)),
                      index=P.index, columns=cols)
    P.open = px
    P.close = px.shift(-1).ffill()           # close of bar t == open of bar t+1
    P.volume = pd.DataFrame(rng.uniform(1, 2, (n_bars, n_coins)), index=P.index, columns=cols)
    P.taker_buy = P.volume / 2
    P.funding = pd.DataFrame(0.0, index=P.index, columns=cols)
    P.ret = P.open.shift(-1) / P.open - 1
    P.active = P.close.notna() & P.ret.notna()
    P.vol = P.close.pct_change().rolling(30, min_periods=10).std()
    P.dfs = {}
    return P


def test_oracle_signal_is_hugely_profitable():
    P = _fake_panel()
    # decided at close t, held during t+1 -> perfect foresight is sign of ret[t+1]
    oracle = np.sign(P.ret.shift(-1)).fillna(0)
    m = E.metrics(E.run(P, E.ts_weights(P, oracle), cost_bps=8))
    assert m["sharpe"] > 10


def test_same_bar_signal_earns_nothing():
    """A signal equal to the return of the bar it is computed on must NOT be
    rewarded — it is only known after that bar closed."""
    P = _fake_panel()
    same_bar = np.sign(P.ret).fillna(0)
    m = E.metrics(E.run(P, E.ts_weights(P, same_bar), cost_bps=0))
    assert abs(m["sharpe"]) < 1.0


def test_random_signal_loses_costs():
    P = _fake_panel(seed=1)
    rng = np.random.default_rng(7)
    rnd = pd.DataFrame(rng.choice([-1.0, 1.0], P.open.shape), index=P.index, columns=P.open.columns)
    free = E.run(P, E.ts_weights(P, rnd), cost_bps=0)["net"].sum()
    paid = E.run(P, E.ts_weights(P, rnd), cost_bps=8)["net"].sum()
    assert paid < free


def test_funding_long_pays_short_receives():
    P = _fake_panel()
    P.funding = P.funding + 0.001
    longs = pd.DataFrame(1.0, index=P.index, columns=P.open.columns)
    assert E.run(P, E.ts_weights(P, longs))["funding"].sum() > 0
    assert E.run(P, E.ts_weights(P, -longs))["funding"].sum() < 0


def test_size_fixed_while_position_held():
    P = _fake_panel()
    longs = pd.DataFrame(1.0, index=P.index, columns=P.open.columns)
    w = E.ts_weights(P, longs)
    held = w.iloc[60:300]
    assert (held.nunique() == 1).all()


def test_xs_books_are_plus_minus_one():
    P = _fake_panel(n_coins=20)
    score = P.close.pct_change(7)
    w = E.xs_weights(P, score, rebal=1)
    row = w.iloc[100]
    assert row.clip(lower=0).sum() == pytest.approx(1.0)
    assert row.clip(upper=0).sum() == pytest.approx(-1.0)


def test_hold_state_machine():
    idx = pd.RangeIndex(6)
    li = pd.Series([1, 0, 0, 0, 0, 0], dtype=bool, index=idx)
    lo = pd.Series([0, 0, 1, 0, 0, 0], dtype=bool, index=idx)
    si = pd.Series([0, 0, 0, 1, 0, 0], dtype=bool, index=idx)
    so = pd.Series([0, 0, 0, 0, 0, 1], dtype=bool, index=idx)
    assert S._hold(li, lo, si, so).tolist() == [1, 1, 0, -1, -1, 0]


def test_timed_hold():
    idx = pd.RangeIndex(6)
    lt = pd.Series([1, 0, 0, 0, 0, 0], dtype=bool, index=idx)
    st = pd.Series([0] * 6, dtype=bool, index=idx)
    assert S._timed(lt, st, 3).tolist() == [1, 1, 1, 0, 0, 0]
