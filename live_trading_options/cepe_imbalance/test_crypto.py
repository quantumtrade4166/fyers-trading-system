"""
cepe_imbalance/test_crypto.py — BTC (Delta) books: sessions, trading day, expiry roll, ticks, a paper entry.

    .venv\\Scripts\\python.exe -m pytest live_trading_options/cepe_imbalance/test_crypto.py -q
"""
import datetime as dt

import pytest

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import crypto
from live_trading_options.cepe_imbalance import engine as EN
from live_trading_options.cepe_imbalance import instruments as I
from live_trading_options.cepe_imbalance.book import IST
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.market import Market
from live_trading_options.cepe_imbalance.st_signal import _days_between

D = dt.date(2026, 11, 14)                                  # a Saturday


def at(day, hhmm):
    return dt.datetime.combine(day, dt.time.fromisoformat(hhmm), IST)


def products(days_ahead=(0, 1), strikes=range(68000, 72001, 200)):
    """Synthetic Delta products settling 17:30 IST (12:00 UTC) on today + days_ahead (real clock)."""
    today = dt.datetime.now(IST).date()
    out = []
    for n in days_ahead:
        e = today + dt.timedelta(days=n)
        tag = e.strftime("%d%m%y")
        for k in strikes:
            for ct, p in (("call_options", "C"), ("put_options", "P")):
                out.append({"id": 1, "symbol": f"{p}-BTC-{k}-{tag}", "contract_type": ct, "strike_price": str(k),
                            "settlement_time": f"{e.isoformat()}T12:00:00Z", "contract_value": "0.001",
                            "tick_size": "0.1"})
    return out


@pytest.fixture
def universe():
    u = I.build({})
    crypto.attach(u, products(), keep_settled=True)
    return u


# ── sessions / trading day ─────────────────────────────────────────────────
def test_indian_session():
    o, c, sq = EN.session("BTC", D)
    assert (o, c, sq) == (at(D, "09:00"), at(D, "17:30"), at(D, "17:20"))
    assert EN.session("BTC_ST", D) == (o, c, sq)


def test_full_expiry_session_opens_the_evening_before():
    o, c, sq = EN.session("BTCFULL", D)
    assert o == at(D - dt.timedelta(days=1), "17:30") and c == at(D, "17:30") and sq == at(D, "17:20")


def test_trading_day_rolls_at_1730_only_for_full_expiry():
    assert EN.trading_day("BTCFULL", at(D, "17:29")) == D
    assert EN.trading_day("BTCFULL", at(D, "17:30")) == D + dt.timedelta(days=1)
    assert EN.trading_day("BTCFULL", at(D, "23:59")) == D + dt.timedelta(days=1)
    assert EN.trading_day("BTC", at(D, "23:59")) == D
    assert EN.trading_day("NIFTY", at(D, "23:59")) == D


def test_auto_times_and_currency():
    assert EN.default_auto("BTC")["auto_time"] == "09:00" and EN.default_auto("BTC_ST")["auto_time"] == "09:01"
    assert EN.default_auto("BTCFULL")["auto_time"] == "17:31"
    assert EN.default_auto("NIFTY")["auto_time"] == "09:20"           # unchanged
    assert C.ccy_of("BTCFULL_ST") == "USD" and C.ccy_of("NIFTY") == "INR"
    assert EN.every_day("BTC") and not EN.every_day("CRUDEOIL")


def test_days_between_crosses_midnight():
    t0, t1 = at(D, "17:30").timestamp(), at(D + dt.timedelta(days=1), "08:45").timestamp()
    assert _days_between(t0, t1) == ["2026-11-14", "2026-11-15"]
    assert _days_between(t0, t0 + 60) == ["2026-11-14"]


# ── contracts ──────────────────────────────────────────────────────────────
def test_attach_contracts(universe):
    for n in ("BTC", "BTCFULL"):
        u = universe.underlyings[n]
        assert u.lot * u.mult == pytest.approx(1.0)                   # 1000 contracts x 0.001 BTC = $1/pt
        assert u.spot_key == crypto.SPOT_KEY and u.settle == "17:30"
    c = universe.by_key[f"delta|C-BTC-70000-{dt.datetime.now(IST).date():%d%m%y}"]
    assert (c.kind, c.strike, c.lot, c.mult) == ("CE", 70000.0, 1000, 0.001)
    assert universe.underlyings["BTC"].chains is not universe.underlyings["BTCFULL"].chains


def test_expiry_drops_at_settle_time(universe):
    now = dt.datetime.now(IST)
    today, tomorrow = now.date().isoformat(), (now.date() + dt.timedelta(days=1)).isoformat()
    first = universe.underlyings["BTC"].expiries()[0]
    assert first == (today if now.strftime("%H:%M") < "17:30" else tomorrow)


def test_sample_file_parses():
    import json
    from pathlib import Path
    rows = json.loads((Path(__file__).parent / "testdata" / "delta_products_sample.json").read_text(encoding="utf-8"))
    cs = crypto.contracts(rows, keep_settled=True)
    assert len(cs) > 300 and {c.kind for c in cs} == {"CE", "PE"}
    assert crypto.settle_date("2026-11-14T12:00:00Z") == "2026-11-14"


# ── ticks ──────────────────────────────────────────────────────────────────
def test_decode_mark_and_spot():
    t = crypto.decode({"type": "mark_price", "symbol": "MARK:C-BTC-67400-141126", "price": "239.55",
                       "best_bid": "238", "best_ask": "240", "bid_qty": "8517", "ask_qty": "6130"}, 1000.5)
    assert t == Tick("delta|C-BTC-67400-141126", 239.55, 238.0, 240.0, 8517, 6130, None, None, 1000, 1000.5)
    s = crypto.decode({"type": "spot_price", "symbol": ".DEXBTUSD", "price": 67312.6}, 5.0)
    assert s.key == "delta|.DEXBTUSD" and s.ltp == 67312.6
    assert crypto.decode({"type": "v2/ticker"}, 1.0) is None
    empty = crypto.decode({"type": "mark_price", "symbol": "MARK:X", "price": "5", "best_bid": "0", "best_ask": None}, 1.0)
    assert empty.bid is None and empty.ask is None


def test_composite_feed_routes_keys():
    class F:
        def __init__(self):
            self.got = []

        def want(self, keys):
            self.got += keys
            return len(keys)

        def status(self):
            return {"authed": True}
    k, d = F(), F()
    cf = crypto.CompositeFeed(k, d)
    assert cf.want(["nse_cm|Nifty 50", "delta|.DEXBTUSD", "mcx_fo|1", "delta|C-BTC-1-1"]) == 4
    assert k.got == ["nse_cm|Nifty 50", "mcx_fo|1"] and d.got == ["delta|.DEXBTUSD", "delta|C-BTC-1-1"]
    assert cf.status() == {"authed": True, "delta": {"authed": True}}


# ── engine: a paper entry in USD ───────────────────────────────────────────
def test_btc_normal_entry_in_usd(universe):
    m = Market(universe)
    e = EN.Engine(universe, m, persist=False)
    assert {"BTC", "BTC_ST", "BTCFULL", "BTCFULL_ST"} <= set(e.runners)
    exp = m.expiry("BTC")
    ticks = [Tick(crypto.SPOT_KEY, 70030.0, None, None, None, None, None, None, 0, 0.0)]
    for c in (x for row in universe.underlyings["BTC"].chains[exp].values() for x in row.values()):
        px = max(1000 - abs(c.strike - 70000) * 0.8, 50.0)
        ticks.append(Tick(c.key, px, px - 2, px + 2, 100, 100, None, None, 0, 0.0))
    e.refresh_subscriptions()
    m.apply(ticks)
    assert m.atm("BTC", exp) == 70000.0
    e.start("BTC")
    r = e.runners["BTC"]
    shorts = sorted((p.kind, p.strike, p.lots) for p in r.book.positions.values() if p.lots)
    if not r.ctx.in_session():                                  # outside 09:00-17:20 IST the rules wait
        assert shorts == []
        return
    assert shorts == [("CE", 70000.0, -1), ("CE", 70200.0, -1), ("CE", 70400.0, -1),
                      ("PE", 69600.0, -1), ("PE", 69800.0, -1), ("PE", 70000.0, -1)]
    im = r.book.imbalance()
    assert im["pct"] == pytest.approx(0.0, abs=0.01)
    assert im["ce_value"] == pytest.approx((1000 + 840 + 680) * 1.0)    # premium x 1 lot x 1000 x 0.001
    row = next(x for x in e.summary()["underlyings"] if x["name"] == "BTC")
    assert row["ccy"] == "USD" and row["kind"] == "crypto"


def test_full_expiry_book_rolls_at_1730_not_midnight(universe):
    m = Market(universe)
    e = EN.Engine(universe, m, persist=False)
    for n in e.runners:
        e.runners[n].date = EN.book_date(n, at(D, "17:25"))
    e.tick_timer(at(D, "17:31"))
    assert e.runners["BTCFULL"].date == "2026-11-15" and e.runners["BTCFULL_ST"].date == "2026-11-15"
    assert e.runners["BTC"].date == "2026-11-14" and e.runners["NIFTY"].date == "2026-11-14"


def test_crypto_auto_start_on_saturday(universe):
    m = Market(universe)
    e = EN.Engine(universe, m, persist=False)
    started = []
    e.start = lambda n, how="manual": started.append(n)
    sat = at(D, "09:00")
    for n in ("BTC", "NIFTY", "CRUDEOIL"):
        r = e.runners[n]
        r.date = EN.book_date(n, sat)
        e.maybe_auto_start(n, r, sat if n == "BTC" else at(D, "10:00"))
    assert started == ["BTC"]
    r = e.runners["BTCFULL"]                                    # 17:31 Saturday evening, Sunday's book
    e.maybe_auto_start("BTCFULL", r, at(D, "17:31"))
    assert started == ["BTC", "BTCFULL"]
