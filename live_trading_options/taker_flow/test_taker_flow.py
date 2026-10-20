"""
Taker Flow paper broker tests (offline — the order book is faked).
Run: .venv\\Scripts\\python.exe -m pytest live_trading_options/taker_flow/test_taker_flow.py -q
"""

import pytest

from . import paper_broker as B
from .flow_signal import map_binance_to_delta

BOOK = {"buy": [{"price": "99", "size": 10}, {"price": "98", "size": 10}],
        "sell": [{"price": "101", "size": 10}, {"price": "102", "size": 10}]}
INFO = {"symbol": "XUSD", "product_id": 1, "contract_value": 1.0, "taker_fee": 0.0005,
        "mark": 100.0, "funding_rate_8h_pct": 0.01}


@pytest.fixture(autouse=True)
def fake_book(monkeypatch):
    monkeypatch.setattr(B, "l2", lambda sym: BOOK)


def test_walk_book_buy_crosses_levels():
    px, mid, thin = B.walk_book(BOOK, 15)
    assert px == pytest.approx((10 * 101 + 5 * 102) / 15)
    assert mid == 100 and not thin


def test_walk_book_thin_flag():
    px, _, thin = B.walk_book(BOOK, -25)
    assert thin and px == pytest.approx((10 * 99 + 10 * 98 + 5 * 98) / 25)


def test_fee_includes_gst():
    L = B.Ledger(capital=10_000)
    t = B.execute(L, INFO, "X", 10, "t")
    assert t["fee"] == pytest.approx(10 * 101 * 0.0005)
    assert t["gst"] == pytest.approx(t["fee"] * 0.18)
    assert L.equity() == pytest.approx(10_000 + (100 - 101) * 10 - t["fee"] - t["gst"])


def test_reduce_and_flip_realize_correctly():
    L = B.Ledger(capital=10_000)
    B.execute(L, INFO, "X", 10, "open")            # long 10 @ 101
    t = B.execute(L, INFO, "X", -15, "flip")       # sell 15: 10 @ 99, 5 @ 98 -> avg 98.67
    assert L.realized == pytest.approx((t["price"] - 101) * 10)
    p = L.positions["X"]
    assert p.contracts == -5 and p.avg_price == pytest.approx(t["price"])


def test_close_removes_position():
    L = B.Ledger(capital=10_000)
    B.execute(L, INFO, "X", -4, "open")
    B.execute(L, INFO, "X", 4, "close")
    assert "X" not in L.positions


def test_funding_long_pays_short_receives():
    L = B.Ledger(capital=10_000)
    B.execute(L, INFO, "X", 10, "open")
    paid = B.accrue_funding(L, {"X": INFO}, "h1")
    assert paid == pytest.approx(10 * 100 * 0.01 / 100 / 8)
    assert B.accrue_funding(L, {"X": INFO}, "h1") == 0       # same hour never twice
    L2 = B.Ledger(capital=10_000)
    B.execute(L2, INFO, "X", -10, "open")
    assert B.accrue_funding(L2, {"X": INFO}, "h1") < 0


def test_target_contracts_rounds_to_whole_contracts():
    assert B.target_contracts(10_000, 0.5, 0.1, INFO) == 5
    assert B.target_contracts(10_000, 0.5, -0.1, INFO) == -5


def test_symbol_mapping():
    delta = {"PEPE": {}, "DOGE": {}, "PAXG": {}}
    assert map_binance_to_delta("1000PEPEUSDT", delta) == "PEPE"
    assert map_binance_to_delta("DOGEUSDT", delta) == "DOGE"
    assert map_binance_to_delta("PAXGUSDT", delta) is None
    assert map_binance_to_delta("FOOUSDT", delta) is None


def test_ledger_roundtrip(tmp_path):
    L = B.Ledger(capital=10_000)
    B.execute(L, INFO, "X", 3, "open")
    L.save(tmp_path / "l.json")
    L2 = B.Ledger.load(tmp_path / "l.json", 10_000)
    assert L2.positions["X"].contracts == 3 and L2.fees == pytest.approx(L.fees)


def _books_add_up(L):
    lb, sb = L.book("long"), L.book("short")
    total_net = L.equity() - L.capital
    assert lb["net"] + sb["net"] == pytest.approx(total_net, abs=0.02)
    assert lb["charges"] + sb["charges"] == pytest.approx(L.fees + L.gst, abs=0.02)


def test_books_split_and_add_up():
    L = B.Ledger(capital=10_000)
    B.execute(L, INFO, "X", 10, "long")
    B.execute(L, {**INFO, "symbol": "YUSD"}, "Y", -8, "short")
    B.accrue_funding(L, {"X": INFO, "Y": INFO}, "h1")
    assert L.funding_long > 0 > L.funding_short          # long pays, short receives
    assert L.book("long")["positions"] == 1 and L.book("short")["positions"] == 1
    _books_add_up(L)


def test_flip_splits_realized_to_the_closed_side():
    L = B.Ledger(capital=10_000)
    B.execute(L, INFO, "X", 10, "open long")
    B.execute(L, INFO, "X", -15, "flip")
    assert L.realized_long == pytest.approx(L.realized) and L.realized_short == 0
    assert L.charges_short > 0                            # the 5 new short contracts
    _books_add_up(L)


def test_split_existing_rebuilds_like_live_tracking():
    L = B.Ledger(capital=10_000)
    trades = [B.execute(L, INFO, "X", 10, "o"), B.execute(L, INFO, "X", -15, "f"),
              B.execute(L, {**INFO, "symbol": "YUSD"}, "Y", -3, "o")]
    live = (L.charges_long, L.charges_short, L.realized_long, L.realized_short)
    L.charges_long = L.charges_short = L.realized_long = L.realized_short = 0.0
    assert B.split_existing(L, trades, {}, 0)
    assert (L.charges_long, L.charges_short, L.realized_long, L.realized_short) == pytest.approx(live, abs=1e-3)
    assert not B.split_existing(L, trades, {}, 0)         # runs once only
