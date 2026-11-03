"""
cepe_imbalance/test_cepe.py — offline tests (real 2026-11-02 Kotak scrip samples, fake ticks, no network).

    .venv\\Scripts\\python.exe -m pytest live_trading_options/cepe_imbalance/test_cepe.py -q
"""
import datetime as dt
import struct
from pathlib import Path

import pytest

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import instruments as I
from live_trading_options.cepe_imbalance.book import IST, PaperBook
from live_trading_options.cepe_imbalance.engine import Engine, session, us_dst
from live_trading_options.cepe_imbalance.feed import Tick, decode
from live_trading_options.cepe_imbalance.market import Market
from live_trading_options.cepe_imbalance import strategy as S

TD = Path(__file__).resolve().parent / "testdata"
DAY = "2026-11-02"


@pytest.fixture(scope="module")
def universe():
    texts = {s: (TD / f"scrip_{s}_sample.csv").read_text(encoding="utf-8") for s in ("nse_fo", "bse_fo", "mcx_fo")}
    return I.build(texts, today=DAY)


def tick(key, ltp, bid=None, ask=None):
    return Tick(key, ltp, bid, ask, 10, 10, 0, 0, 0, 0.0)


# ── universe ──────────────────────────────────────────────────────────────
def test_all_seven_loaded(universe):
    assert set(universe.underlyings) == set(C.UNDERLYINGS)
    for u in universe.underlyings.values():
        assert u.chains and u.futures, u.name


def test_expiry_encodings(universe):
    assert universe.underlyings["NIFTY"].expiries()[0] == "2026-11-03"      # Tuesday, 1980 epoch
    assert universe.underlyings["SENSEX"].expiries()[0] == "2026-11-05"     # Thursday, BSE IO type
    assert I.expiry_date("nse_fo", 1478183400) == "2026-11-03"
    assert I.expiry_date("bse_fo", 1793903399) == "2026-11-05"


def test_rupee_value_per_point(universe):
    v = {n: u.lot * u.mult for n, u in universe.underlyings.items()}
    assert v == {"NIFTY": 65, "SENSEX": 20, "CRUDEOIL": 100, "GOLDM": 10, "GOLD": 100, "SILVERM": 5, "SILVER": 30}


def test_keys_unique_per_segment(universe):
    assert all(k.split("|")[0] in ("nse_fo", "bse_fo", "mcx_fo") for k in universe.by_key)


# ── feed decode ───────────────────────────────────────────────────────────
NIFTY_IDX = bytes.fromhex("5700271c0101000000484231fd366f23001e2d230032a523004169230012ca2300955ee86a00000000083e280006d82100ae000000acfae19a133a434302010000004e6966747920353020202020202020202020202020")
SENSEX_IDX = bytes.fromhex("5700271c0501000000c7b3ede280b67200c19d71007a4a730054687200b18a7300955ee86a000000000000000000000000a90000000000000000000000020100000053454e534558000000000000000000000000000000")


def test_index_packets_decode():
    names = {("nse_cm", "nifty 50"): "nse_cm|Nifty 50", ("bse_cm", "sensex"): "bse_cm|SENSEX"}
    out = decode(NIFTY_IDX + SENSEX_IDX, {"nse_cm": 100, "bse_cm": 100}, names)
    assert [(t.key, t.ltp) for t in out] == [("nse_cm|Nifty 50", 23454.90), ("bse_cm|SENSEX", 75721.45)]


def test_unknown_index_ignored():
    assert decode(NIFTY_IDX, {"nse_cm": 100}, {}) == []


# ── market ────────────────────────────────────────────────────────────────
def test_atm_from_spot_and_future(universe):
    m = Market(universe)
    m.apply([tick("nse_cm|Nifty 50", 23454.9)])
    assert m.atm("NIFTY") == 23450
    g = universe.underlyings["GOLDM"]
    fut = g.future_for(g.expiries()[0])
    m.apply([tick(fut.key, 152240)])
    assert m.atm("GOLDM") == 152000
    assert len(m.window("NIFTY", m.expiry("NIFTY"), 5)) == 11


def test_synthetic_from_parity(universe):
    m = Market(universe)
    m.apply([tick("nse_cm|Nifty 50", 23440.8)])
    e = m.expiry("NIFTY")
    for k, c, p in ((23400, 53.3, 101.7), (23450, 36.8, 135.0), (23500, 25.1, 172.9)):
        m.apply([tick(m.contract("NIFTY", e, k, "CE").key, c, c - .05, c + .05),
                 tick(m.contract("NIFTY", e, k, "PE").key, p, p - .05, p + .05)])
    assert m.synthetic("NIFTY") == pytest.approx(23351.87, abs=0.01)


# ── book ──────────────────────────────────────────────────────────────────
def _short_strangle(universe):
    m = Market(universe)
    m.apply([tick("nse_cm|Nifty 50", 23450)])
    e = m.expiry("NIFTY")
    ce, pe = m.contract("NIFTY", e, 23600, "CE"), m.contract("NIFTY", e, 23300, "PE")
    m.apply([tick(ce.key, 60, 59.5, 60.5), tick(pe.key, 40, 39.5, 40.5)])
    b = PaperBook("NIFTY", m, DAY, persist=False)
    return m, b, ce, pe


def test_sell_fills_at_bid_buy_at_ask(universe):
    m, b, ce, pe = _short_strangle(universe)
    t = b.order(ce, "SELL", 2)
    assert t["price"] == 59.5 and b.positions[ce.key].lots == -2
    t = b.order(ce, "BUY", 2)
    assert t["price"] == 60.5 and b.positions[ce.key].lots == 0
    assert b.mtm()["realized"] == pytest.approx((59.5 - 60.5) * 2 * 65)


def test_imbalance_only_net_shorts(universe):
    m, b, ce, pe = _short_strangle(universe)
    assert b.imbalance()["pct"] is None
    b.order(ce, "SELL", 1)
    b.order(pe, "SELL", 1)
    # CE 60 vs PE 40 -> (60-40)/100 = 20 %
    assert b.imbalance()["pct"] == pytest.approx(20.0)
    hedge = m.contract("NIFTY", m.expiry("NIFTY"), 24000, "CE")
    m.apply([tick(hedge.key, 5, 4.9, 5.1)])
    b.order(hedge, "BUY", 3)                                  # longs ignored
    assert b.imbalance()["pct"] == pytest.approx(20.0)


def test_unrealized_and_exit_all(universe):
    m, b, ce, pe = _short_strangle(universe)
    b.order(ce, "SELL", 1)                                    # @59.5
    b.order(pe, "SELL", 1)                                    # @39.5
    m.apply([tick(ce.key, 50, 49.5, 50.5), tick(pe.key, 45, 44.5, 45.5)])
    assert b.mtm()["unrealized"] == pytest.approx(((59.5 - 50) + (39.5 - 45)) * 65)
    out = b.exit_all()
    assert all(t["ok"] for t in out) and all(p.lots == 0 for p in b.positions.values())


def test_flip_through_zero(universe):
    m, b, ce, pe = _short_strangle(universe)
    b.order(ce, "SELL", 1)
    b.order(ce, "BUY", 3)
    p = b.positions[ce.key]
    assert p.lots == 2 and p.avg == 60.5


def test_gold_multiplier(universe):
    m = Market(universe)
    g = universe.underlyings["GOLD"]
    e = g.expiries()[0]
    s = g.strikes(e)[len(g.strikes(e)) // 2]
    c = m.contract("GOLD", e, s, "CE")
    m.apply([tick(c.key, 1000, 990, 1010)])
    b = PaperBook("GOLD", m, DAY, persist=False)
    b.order(c, "SELL", 1)
    m.apply([tick(c.key, 900, 890, 910)])
    assert b.mtm()["unrealized"] == pytest.approx((990 - 900) * 100)   # 1 kg lot, priced per 10 g


# ── engine ────────────────────────────────────────────────────────────────
def test_us_dst_and_mcx_hours():
    assert us_dst(dt.date(2026, 10, 30)) and not us_dst(dt.date(2026, 11, 2))
    o, c, sq = session("GOLDM", dt.date(2026, 11, 2))
    assert (o.strftime("%H:%M"), c.strftime("%H:%M"), sq.strftime("%H:%M")) == ("09:00", "23:55", "23:45")
    o, c, sq = session("NIFTY", dt.date(2026, 11, 2))
    assert (c.strftime("%H:%M"), sq.strftime("%H:%M")) == ("15:30", "15:15")


class _Rec(S.Strategy):
    def __init__(self):
        self.ticks, self.timers, self.ended = [], 0, False

    def on_tick(self, ctx, keys):
        self.ticks.append(keys)

    def on_timer(self, ctx):
        self.timers += 1

    def on_session_end(self, ctx):
        self.ended = True
        super().on_session_end(ctx)


class _Boom(S.Strategy):
    def on_tick(self, ctx, keys):
        raise ValueError("bad rule")


def test_engine_routes_ticks_and_session_end(universe):
    m = Market(universe)
    eng = Engine(universe, m, persist=False)
    m.apply([tick("nse_cm|Nifty 50", 23450)])
    eng.refresh_subscriptions()
    r = eng.runners["NIFTY"]
    r.strategy = _Rec()
    eng.start("NIFTY")
    ce = m.contract("NIFTY", m.expiry("NIFTY"), 23500, "CE")
    eng.on_ticks([tick(ce.key, 80, 79.5, 80.5), tick("nse_cm|Nifty 50", 23452)])
    assert r.strategy.ticks and ce.key in r.strategy.ticks[-1]
    eng.manual_order("NIFTY", 23500, "CE", "SELL", 1)
    day = dt.date.fromisoformat(r.date)                       # the engine's own day (no rollover)
    eng.tick_timer(dt.datetime.combine(day, dt.time(10, 0), IST))
    assert r.strategy.timers == 1
    eng.tick_timer(dt.datetime.combine(day, dt.time(15, 16), IST))
    assert r.strategy.ended and r.book.positions[ce.key].lots == 0
    eng.on_ticks([tick(ce.key, 81, 80.5, 81.5)])          # after square-off: no more rule calls
    n = len(r.strategy.ticks)
    eng.on_ticks([tick(ce.key, 82, 81.5, 82.5)])
    assert len(r.strategy.ticks) == n


def test_strategy_error_isolated(universe):
    m = Market(universe)
    eng = Engine(universe, m, persist=False)
    m.apply([tick("nse_cm|Nifty 50", 23450)])
    eng.refresh_subscriptions()
    eng.runners["NIFTY"].strategy = _Boom()
    eng.start("NIFTY")
    eng.on_ticks([tick("nse_cm|Nifty 50", 23455)])
    assert eng.runners["NIFTY"].errors == 1


def test_stopped_runner_gets_no_ticks(universe):
    m = Market(universe)
    eng = Engine(universe, m, persist=False)
    m.apply([tick("nse_cm|Nifty 50", 23450)])
    eng.refresh_subscriptions()
    r = eng.runners["NIFTY"]
    r.strategy = _Rec()
    eng.on_ticks([tick("nse_cm|Nifty 50", 23455)])
    assert r.strategy.ticks == []
