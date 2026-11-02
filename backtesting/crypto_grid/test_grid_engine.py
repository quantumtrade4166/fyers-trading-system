import numpy as np
import pandas as pd

from backtesting.crypto_grid.grid_engine import GridConfig, run_grid


def _path(prices):
    p = np.asarray(prices, float)
    day = np.repeat(np.datetime64("2024-01-01", "ns"), len(p))
    last = np.zeros(len(p), bool); last[-1] = True
    return p, day, last


def test_round_trips_pay_one_step_each():
    # start 100, grid 1%: wobble down one level and back up 5 times
    up, dn = 100.0, 100.0 / 1.01
    p, d, l = _path([100] + [dn, up] * 5)
    cfg = GridConfig(step=0.01, half_range=0.05, style="neutral", restart="none",
                     maker_fee=0.0, taker_fee=0.0)
    daily, st = run_grid(p, d, l, cfg)
    R = st["levels"] // 2
    Q = 10_000 / R
    assert st["round_trips"] == 5
    assert abs(daily["equity"].iloc[-1] - (10_000 + 5 * Q * 0.01)) < 1e-6


def test_no_fills_without_level_cross():
    p, d, l = _path([100, 100.4, 99.7, 100.2])
    cfg = GridConfig(step=0.01, half_range=0.05, style="neutral", restart="none",
                     maker_fee=0.0, taker_fee=0.0)
    daily, st = run_grid(p, d, l, cfg)
    assert st["round_trips"] == 0 and abs(daily["equity"].iloc[-1] - 10_000) < 1e-6


def test_long_grid_below_range_holds_all_coins():
    # spot grid: price falls 30% below a +-10% window -> fully in coins, loses
    p, d, l = _path(np.linspace(100, 70, 200))
    cfg = GridConfig(step=0.01, half_range=0.10, style="long", restart="none",
                     maker_fee=0.0, taker_fee=0.0)
    daily, st = run_grid(p, d, l, cfg)
    assert daily["position"].iloc[-1] > 0
    assert daily["equity"].iloc[-1] < 10_000 * 0.85


def test_fees_reduce_profit():
    up, dn = 100.0, 100.0 / 1.01
    p, d, l = _path([100] + [dn, up] * 20)
    base = dict(step=0.01, half_range=0.05, style="neutral", restart="none")
    e0 = run_grid(p, d, l, GridConfig(**base, maker_fee=0, taker_fee=0))[0]["equity"].iloc[-1]
    e1 = run_grid(p, d, l, GridConfig(**base, maker_fee=0.001, taker_fee=0.001))[0]["equity"].iloc[-1]
    assert e1 < e0


def test_filter_off_stays_in_cash():
    up, dn = 100.0, 100.0 / 1.01
    p, d, l = _path([100] + [dn, up] * 5)
    cfg = GridConfig(step=0.01, half_range=0.05, style="long", restart="exit")
    daily, st = run_grid(p, d, l, cfg, active=np.zeros(len(p), bool))
    assert st["round_trips"] == 0 and daily["equity"].iloc[-1] == 10_000


def test_filter_switch_off_closes_position():
    p, d, l = _path(np.linspace(100, 90, 50))
    act = np.ones(len(p), bool); act[25:] = False
    cfg = GridConfig(step=0.01, half_range=0.2, style="long", restart="none")
    daily, _ = run_grid(p, d, l, cfg, active=act)
    assert daily["position"].iloc[-1] == 0.0
