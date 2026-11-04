"""
cepe_imbalance/test_strategy.py — the owner's CE/PE imbalance rules on CRUDEOIL (grid 200), offline.

Prices come from a toy model: intrinsic + 150 * exp(-((K-F)/500)^2), bid/ask +-0.5.

    .venv\\Scripts\\python.exe -m pytest live_trading_options/cepe_imbalance/test_strategy.py -q
"""
import math
from pathlib import Path

import pytest

from live_trading_options.cepe_imbalance import instruments as I
from live_trading_options.cepe_imbalance.engine import Engine
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.market import Market
from live_trading_options.cepe_imbalance.strategy import CePeImbalance

TD = Path(__file__).resolve().parent / "testdata"
N = "CRUDEOIL"


@pytest.fixture(scope="module")
def universe():
    texts = {s: (TD / f"scrip_{s}_sample.csv").read_text(encoding="utf-8") for s in ("nse_fo", "bse_fo", "mcx_fo")}
    return I.build(texts, today="2026-11-02")


def model(k, kind, f):
    intr = max(f - k, 0) if kind == "CE" else max(k - f, 0)
    return round(intr + 150 * math.exp(-((k - f) / 500) ** 2), 1)


class Rig:
    def __init__(self, universe, f, override=None):
        self.m = Market(universe)
        self.eng = Engine(universe, self.m, persist=False)
        self.r = self.eng.runners[N]
        self.r.ctx.in_session = lambda now=None: True
        self.e = self.m.expiry(N)
        self.fut = universe.underlyings[N].future_for(self.e)
        self.move(f, override, run=False)
        self.eng.refresh_subscriptions()

    def move(self, f, override=None, run=True):
        override = override or {}
        ticks = [Tick(self.fut.key, f, f - 1, f + 1, 1, 1, 0, 0, 0, 0.0)]
        for k in range(8000, 11001, 50):
            for kind in ("CE", "PE"):
                c = self.m.contract(N, self.e, k, kind)
                if c:
                    px = override.get((k, kind), model(k, kind, f))
                    ticks.append(Tick(c.key, px, px - 0.5, px + 0.5, 1, 1, 0, 0, 0, 0.0))
        if run:
            self.eng.on_ticks(ticks)
        else:
            self.m.apply(ticks)

    def legs(self):
        return {k: dict(sorted(v.items())) for k, v in CePeImbalance.legs(self.r.ctx).items()}

    def imb(self):
        return self.r.book.imbalance()["pct"]

    def actions(self):
        return [e["action"] for e in self.r.events if e["event"] == "adjust"]


def invariants(rig):
    lg, atm = rig.legs(), rig.r.state["atm"]
    assert lg["CE"].get(atm) and lg["PE"].get(atm), f"not short at ATM {atm}: {lg}"
    assert sum(lg["CE"].values()) == sum(lg["PE"].values()), f"lots unequal: {lg}"


# ── entry ─────────────────────────────────────────────────────────────────
def test_entry_six_strikes(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    assert g.r.state["phase"] == "in"
    assert g.legs() == {"CE": {9400: 1, 9600: 1, 9800: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}
    assert abs(g.imb()) < 1
    invariants(g)


def test_entry_pre_check_uses_B_not_sell_then_buy(universe):
    up = {(9400, "CE"): 300.0, (9600, "CE"): 250.0, (9800, "CE"): 160.0}      # CE far richer
    g = Rig(universe, 9400, up)
    g.eng.start(N)
    lg = g.legs()
    assert lg["CE"] == {9400: 1, 9600: 1, 9800: 1}
    assert 9000 not in lg["PE"] and sum(lg["PE"].values()) == 3
    assert all(t["side"] == "SELL" for t in g.r.book.trades)                 # nothing bought back
    invariants(g)


# ── hard ATM rule + A ─────────────────────────────────────────────────────
def test_market_up_atm_rule_then_A(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    g.move(9545)                                   # ATM 9400 -> 9600 (past 9500 + 20% buffer)
    assert g.r.state["atm"] == 9600
    assert g.legs()["PE"] == {9200: 1, 9400: 1, 9600: 1}                     # farthest PE 9000 -> ATM
    assert g.legs()["CE"] == {9400: 1, 9600: 1, 9800: 1}                     # ITM 9400 CE sits
    assert abs(g.imb()) <= 15
    g.move(9600)                                   # clean set at 9600 is balanced -> straight back to normal
    assert any(a.startswith("back to normal") for a in g.actions()), g.actions()
    assert g.legs()["CE"] == {9600: 1, 9800: 1, 10000: 1}
    assert abs(g.imb()) <= 15
    invariants(g)


def test_market_down_is_mirror(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    g.move(9255)
    assert g.r.state["atm"] == 9200
    assert g.legs()["CE"] == {9200: 1, 9400: 1, 9600: 1}
    g.move(9200)
    assert any(a.startswith("back to normal") for a in g.actions()), g.actions()
    assert g.legs()["PE"] == {8800: 1, 9000: 1, 9200: 1}
    invariants(g)


def test_trend_keeps_rolling_and_stays_at_atm(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    for f in range(9400, 10401, 25):
        g.move(f)
        invariants(g)
        assert abs(g.imb()) <= 15 or g.r.state.get("blocked")
    for f in range(10400, 8999, -25):
        g.move(f)
        invariants(g)


# ── B ─────────────────────────────────────────────────────────────────────
def test_B_when_A_not_possible(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    rich = {(9400, "CE"): 210.0, (9600, "CE"): 190.0, (9800, "CE"): 120.0}  # skew, ATM unchanged
    g.move(9400, rich)
    assert any(a.startswith("B (CE") for a in g.actions())
    assert g.legs()["PE"] == {9200: 2, 9400: 1}                              # 9000 moved in to 9200
    assert g.legs()["CE"] == {9400: 1, 9600: 1, 9800: 1}
    invariants(g)


def test_B_stops_at_atm(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    huge = {(9400, "CE"): 900.0, (9600, "CE"): 800.0, (9800, "CE"): 700.0}
    g.move(9400, huge)
    assert g.legs()["PE"] == {9400: 1, 9600: 2}       # B to ATM, then C puts 2 of 3 units at ITM1
    assert "no A, B or C left" in (g.r.state.get("blocked") or "")
    invariants(g)


# ── back to normal ────────────────────────────────────────────────────────
def test_op_normal_resplits_doubled_side():
    s = CePeImbalance()
    legs = {"CE": {9600: 1, 9800: 1, 10000: 1}, "PE": {9400: 1, 9600: 2}}
    ops = s.op_normal(legs, 9600, 200, 1)
    assert s.apply_ops(legs, ops)["PE"] == {9200: 1, 9400: 1, 9600: 1}
    assert ops[0][2] == "BUY"                                               # buy-back before sell


def test_back_to_normal_after_A(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    rich = {(9400, "CE"): 210.0, (9600, "CE"): 190.0, (9800, "CE"): 120.0}
    g.move(9400, rich)                                                      # B -> PE 9200 x2
    assert g.legs()["PE"] == {9200: 2, 9400: 1}
    g.move(9545)                                   # ATM 9600: ATM rule moves a 9200 PE to 9600
    g.move(9640)                                   # A on CE; then imbalance near 0 -> re-split
    lg = g.legs()
    assert all(v == 1 for v in lg["PE"].values()), lg
    invariants(g)


# ── ATM buffer, stop loss, session end ────────────────────────────────────
def test_atm_buffer_no_flip_flop(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    for f in (9505, 9495, 9530, 9470, 9535):       # around the 9500 half-way point
        g.move(f)
    assert g.r.state["atm"] == 9400
    assert not [a for a in g.actions() if a.startswith("ATM rule")]          # ATM never flipped


def test_mtm_stop_loss(universe):
    g = Rig(universe, 9400)
    g.eng.set_params(N, {"mtm_stop": 1000})
    g.eng.start(N)
    g.move(9400, {(9400, "CE"): 200.0, (9400, "PE"): 200.0})               # both ATM legs +~50 -> loss
    assert g.r.state["phase"] == "stopped"
    assert all(p.lots == 0 for p in g.r.book.positions.values())
    n = len(g.r.book.trades)
    g.move(9600)
    assert len(g.r.book.trades) == n                                        # no re-entry


def test_lots_param(universe):
    g = Rig(universe, 9400)
    g.eng.set_params(N, {"lots": 2})
    g.eng.start(N)
    assert g.legs()["CE"] == {9400: 2, 9600: 2, 9800: 2}
    g.move(9545)
    invariants(g)


def test_session_end_exits(universe):
    import datetime as dt
    from live_trading_options.cepe_imbalance.book import IST
    g = Rig(universe, 9400)
    g.eng.start(N)
    day = dt.date.fromisoformat(g.r.date)
    g.eng.tick_timer(dt.datetime.combine(day, dt.time(23, 56), IST))
    assert all(p.lots == 0 for p in g.r.book.positions.values())
    assert g.r.state["phase"] == "done"


# ── auto-start + durable settings ─────────────────────────────────────────
def _at(g, hhmm):
    import datetime as dt
    from live_trading_options.cepe_imbalance.book import IST
    day = dt.date.fromisoformat(g.r.date)
    if day.weekday() >= 5:
        pytest.skip("auto-start is Mon-Fri; today is a weekend")
    return dt.datetime.combine(day, dt.time.fromisoformat(hhmm), IST)


def test_auto_start_once_at_time(universe):
    g = Rig(universe, 9400)
    g.eng.tick_timer(_at(g, "09:04"))
    assert not g.r.running
    g.eng.tick_timer(_at(g, "09:05"))
    assert g.r.running and g.r.state["phase"] == "in"                      # entered on live prices
    assert [e["how"] for e in g.r.events if e["event"] == "start"] == ["auto"]
    g.eng.stop(N)
    g.eng.tick_timer(_at(g, "09:30"))
    assert not g.r.running                                                  # manual stop sticks


def test_auto_start_off_and_after_square_off(universe):
    g = Rig(universe, 9400)
    g.eng.set_auto(N, auto_start=False)
    g.eng.tick_timer(_at(g, "10:00"))
    assert not g.r.running
    g2 = Rig(universe, 9400)
    g2.eng.tick_timer(_at(g2, "23:50"))                                    # past square-off: no start
    assert not g2.r.running


def test_settings_survive_new_day(universe, tmp_path, monkeypatch):
    from live_trading_options.cepe_imbalance import config as C
    from live_trading_options.cepe_imbalance.engine import Runner
    monkeypatch.setattr(C, "DATA", tmp_path)
    m = Market(universe)
    eng = Engine(universe, m, persist=True)
    eng.set_params(N, {"lots": 3, "mtm_stop": 5000})
    eng.set_auto(N, auto_start=False, auto_time="10:15")
    nxt = Runner(N, m, date="2099-01-05", persist=True)                     # a later day
    assert nxt.params["lots"] == 3 and nxt.params["mtm_stop"] == 5000
    assert nxt.auto == {"auto_start": False, "auto_time": "10:15"}
    with pytest.raises(ValueError):
        eng.set_auto(N, auto_time="25:99")


# ── liquidity guard: never sell into a wide quote ─────────────────────────
def _quote(g, k, kind, bid, ask, ltp=None):
    c = g.m.contract(N, g.e, k, kind)
    g.eng.on_ticks([Tick(c.key, ltp or (bid + ask) / 2, bid, ask, 1, 1, 0, 0, 0, 0.0)])


def test_entry_waits_for_tight_quote(universe):
    g = Rig(universe, 9400)
    _quote(g, 9600, "CE", 100.0, 160.0, 128.0)                             # 46% gap
    g.eng.start(N)
    assert g.r.state["phase"] == "wait_entry" and not g.r.book.trades
    assert "quote too wide" in g.r.state["blocked"] and "9600 CE" in g.r.state["blocked"]
    n_logged = sum(1 for e in g.r.events if e["event"] == "blocked")
    _quote(g, 9600, "CE", 101.0, 161.0, 128.0)                             # still wide: no new log line
    assert sum(1 for e in g.r.events if e["event"] == "blocked") == n_logged
    _quote(g, 9600, "CE", 127.5, 128.5)                                    # tight -> enters on this tick
    assert g.r.state["phase"] == "in" and len(g.r.book.trades) == 6
    assert g.r.state["blocked"] is None


def test_exits_never_wait_on_wide_quotes(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    for k, kind in ((9400, "CE"), (9600, "CE"), (9800, "CE"), (9000, "PE"), (9200, "PE"), (9400, "PE")):
        c = g.m.contract(N, g.e, k, kind)
        g.m.apply([Tick(c.key, 150.0, 100.0, 200.0, 1, 1, 0, 0, 0, 0.0)])  # every quote wide
    out = g.r.book.exit_all("test")
    assert all(t["ok"] for t in out) and all(p.lots == 0 for p in g.r.book.positions.values())


def test_atm_rule_waits_on_wide_sell(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    n = len(g.r.book.trades)
    f = 9545
    g.r.ctx.in_session = lambda now=None: False                            # load prices without acting
    g.move(f)
    c = g.m.contract(N, g.e, 9600, "PE")
    g.m.apply([Tick(c.key, 150.0, 100.0, 200.0, 1, 1, 0, 0, 0, 0.0)])
    g.r.ctx.in_session = lambda now=None: True
    g.eng.on_ticks([Tick(g.fut.key, f, f - 1, f + 1, 1, 1, 0, 0, 0, 0.0)])
    assert "quote too wide" in g.r.state["blocked"] and 9600 not in g.legs()["PE"]
    assert g.actions()[-1].startswith("A (CE heavy")                       # 4-Nov: kept balancing meanwhile
    assert 9400 not in g.legs()["CE"]                                      # ITM CE rolled out
    m = len(g.r.book.trades)
    _quote(g, 9600, "PE", 149.5, 150.5)
    new = [(t["side"], t["strike"], t["kind"]) for t in g.r.book.trades[m:m + 2]]
    assert new == [("BUY", 9000.0, "PE"), ("SELL", 9600.0, "PE")]          # ATM rule went through once tight
    assert g.legs()["PE"].get(9600) == 1


def test_no_adjustment_while_a_leg_has_no_price(universe):
    """Restart case: CE prices arrive before PE -> must NOT read +100% and adjust."""
    g = Rig(universe, 9400)
    g.eng.start(N)
    n = len(g.r.book.trades)
    c = g.m.contract(N, g.e, 9000, "PE")
    g.m.quotes.pop(c.key)                                                   # one short leg unpriced
    g.eng.on_ticks([Tick(g.fut.key, 9400, 9399, 9401, 1, 1, 0, 0, 0, 0.0)])
    im = g.r.book.imbalance()
    assert im["pct"] is None and im["missing"] == ["9000 PE"]
    assert len(g.r.book.trades) == n and "waiting for prices" in g.r.state["blocked"]
    g.move(9400)                                                            # price arrives -> normal again
    assert g.r.book.imbalance()["pct"] is not None and g.r.state["blocked"] is None


def test_history_records_atm_premium(universe):
    import datetime as dt
    from live_trading_options.cepe_imbalance.book import IST
    g = Rig(universe, 9400)
    g.eng.start(N)
    g.eng.sample(dt.datetime.now(IST))
    row = g.eng.hist[N][-1]
    assert row["atm"] == 9400 and row["ace"] == model(9400, "CE", 9400) and row["ape"] == model(9400, "PE", 9400)



# ── C: light side all at ATM -> ITM1, one unit at a time ─────────────────
def test_C_moves_one_unit_then_second(universe):
    s = CePeImbalance()
    legs = {"CE": {9400: 3}, "PE": {9000: 1, 9200: 1, 9400: 1}}
    ops = s.op_C(legs, "PE", 9400, 200, 1)                                  # PE heavy -> CE to ITM1 9200
    assert ops == [("CE", 9400, "BUY", 1), ("CE", 9200, "SELL", 1)]
    legs = s.apply_ops(legs, ops)
    legs = s.apply_ops(legs, s.op_C(legs, "PE", 9400, 200, 1))
    assert legs["CE"] == {9200: 2, 9400: 1}
    assert s.op_C(legs, "PE", 9400, 200, 1) is None                          # never more than 2 at ITM1
    assert s.op_C({"CE": {9400: 2, 9600: 1}, "PE": {}}, "PE", 9400, 200, 1) is None   # B still possible first


def test_C_live_then_back_to_normal(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    rich_pe = {(9400, "PE"): 400.0, (9200, "PE"): 330.0, (9000, "PE"): 260.0}
    g.move(9400, rich_pe)                          # PE heavy: B pulls CEs to ATM, then C to ITM1
    acts = g.actions()
    assert any(a.startswith("C (PE") for a in acts), acts
    assert 9200 in g.legs()["CE"]
    invariants(g)
    calm = {(9200, "CE"): 200.0, (9400, "CE"): 150.0, (9600, "CE"): 200.0, (9800, "CE"): 170.0,
            (9400, "PE"): 250.0, (9200, "PE"): 180.0, (9000, "PE"): 130.0}
    g.move(9400, calm)                             # imbalance back inside +-5% (clean set would be too)
    acts = g.actions()
    assert any(a.startswith("back to normal") for a in acts), acts
    assert g.legs()["CE"] == {9400: 1, 9600: 1, 9800: 1}
    invariants(g)


def test_expiry_morning_entry_uses_C(universe):
    """3-Nov NIFTY case: ATM just above spot on expiry day -> ATM PE double the ATM CE."""
    exp = {(9400, "CE"): 45.0, (9600, "CE"): 40.0, (9800, "CE"): 35.0,
           (9400, "PE"): 90.0, (9200, "PE"): 60.0, (9000, "PE"): 40.0, (9200, "CE"): 160.0}
    g = Rig(universe, 9400, exp)
    g.eng.start(N)
    t = g.r.book.trades
    assert t and all(x["side"] == "SELL" for x in t)                        # balanced before selling
    lg = g.legs()
    assert lg["CE"].get(9200), lg                                           # pre-C put CE at ITM1
    assert abs(g.imb()) <= 15
    invariants(g)


# ── stale LTP + reset ─────────────────────────────────────────────────────
def test_mark_keeps_stale_ltp_inside_quote(universe):
    from live_trading_options.cepe_imbalance.market import Quote
    assert Quote(ltp=8559.5, bid=6402.5, ask=6999.0).mark() == 6999.0      # SILVER 182000 CE, 3-Nov
    assert Quote(ltp=5000.0, bid=6402.5, ask=6999.0).mark() == 6402.5
    assert Quote(ltp=6500.0, bid=6402.5, ask=6999.0).mark() == 6500.0
    assert Quote(ltp=0.0, bid=10.0, ask=12.0).mark() == 11.0
    assert Quote(ltp=23450.0).mark() == 23450.0                             # index: no quote, LTP as is


def test_mtm_and_imbalance_use_mark(universe):
    g = Rig(universe, 9400)
    g.eng.start(N)
    c = g.m.contract(N, g.e, 9800, "CE")
    before = g.imb()
    g.m.apply([Tick(c.key, 500.0, 78.5, 79.5, 1, 1, 0, 0, 0, 0.0)])      # stale LTP 500 vs quote 79
    assert abs(g.imb() - before) < 1.0                                      # unchanged by the stale print


def test_reset_archives_and_starts_fresh(universe, tmp_path, monkeypatch):
    from live_trading_options.cepe_imbalance import config as C
    monkeypatch.setattr(C, "DATA", tmp_path)
    g = Rig(universe, 9400)
    eng = Engine(universe, g.m, persist=True)
    r = eng.runners[N]
    r.ctx.in_session = lambda now=None: True
    eng.refresh_subscriptions()
    eng.start(N)
    assert len(r.book.trades) == 6
    out = eng.reset(N)
    nr = eng.runners[N]
    assert nr is not r and out["ok"] and "CRUDEOIL_trades.jsonl" in out["archived"]
    arch = list((tmp_path / "books" / r.date).glob("_reset_*"))
    assert arch and (arch[0] / "CRUDEOIL_trades.jsonl").exists()
    nr.ctx.in_session = lambda now=None: True
    nr.strategy.evaluate(nr.ctx)                                            # fresh entry
    assert len(nr.book.trades) == 6 and nr.state["auto_done"]



def test_go_normal_unwinds_itm_vs_b_clash(universe):
    """3-Nov NIFTY: ITM CE on one side + B-doubled PE on the other, imbalance 'fine' -> go clean."""
    g = Rig(universe, 9400)
    g.eng.start(N)
    clash = [("CE", 9400, "BUY", 1), ("CE", 9200, "SELL", 1),               # CE: 9200 (ITM) 9600 9800
             ("PE", 9000, "BUY", 1), ("PE", 9200, "SELL", 1)]               # PE: 9200 x2, 9400
    g.r.ctx.in_session = lambda now=None: False
    for kind, k, side, lots in clash:
        g.r.book.order(g.m.contract(N, g.e, k, kind), side, lots, "setup")
    assert g.legs() == {"CE": {9200: 1, 9600: 1, 9800: 1}, "PE": {9200: 2, 9400: 1}}
    g.r.ctx.in_session = lambda now=None: True
    g.move(9400)
    assert g.legs() == {"CE": {9400: 1, 9600: 1, 9800: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}
    assert any(a.startswith("back to normal") for a in g.actions())


def test_itm_stays_when_going_clean_would_hurt(universe):
    """ITM leg + other side normal: if the clean set prices outside +-5%, keep the ITM leg."""
    s = CePeImbalance()
    legs = {"CE": {9200: 1, 9600: 1, 9800: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}
    ops = s.op_go_normal(legs, 9400, 200, 1)
    assert ops == [("CE", 9200, "BUY", 1), ("CE", 9400, "SELL", 1)]          # what going clean would trade
    g = Rig(universe, 9400, {(9400, "CE"): 60.0, (9600, "CE"): 50.0, (9800, "CE"): 40.0})  # CE side cheap
    g.r.ctx.in_session = lambda now=None: False
    for kind, k, side in (("CE", 9200, "SELL"), ("CE", 9600, "SELL"), ("CE", 9800, "SELL"),
                          ("PE", 9000, "SELL"), ("PE", 9200, "SELL"), ("PE", 9400, "SELL")):
        g.r.book.order(g.m.contract(N, g.e, k, kind), side, 1, "setup")
    g.r.state.update(phase="in", atm=9400)
    g.r.running = True
    g.r.ctx.in_session = lambda now=None: True
    n = len(g.r.book.trades)
    g.move(9400, {(9400, "CE"): 60.0, (9600, "CE"): 50.0, (9800, "CE"): 40.0})
    assert not [t for t in g.r.book.trades[n:] if t["strike"] == 9200 and t["kind"] == "CE"]   # ITM CE kept


def test_recentre_when_both_sides_doubled(universe):
    """3-Nov NIFTY 10:53: CE 23650x2 + PE 23600x2 (B on both sides) at -1% -> rebuild like entry."""
    px = {(9400, "CE"): 150.0, (9600, "CE"): 100.0, (9800, "CE"): 60.0,
          (9400, "PE"): 150.0, (9200, "PE"): 130.0, (9000, "PE"): 110.0}
    g = Rig(universe, 9400, px)
    g.r.ctx.in_session = lambda now=None: False
    for kind, k, lots in (("CE", 9400, 2), ("CE", 9600, 1), ("PE", 9400, 1), ("PE", 9200, 2)):
        g.r.book.order(g.m.contract(N, g.e, k, kind), "SELL", lots, "setup")
    g.r.state.update(phase="in", atm=9400)
    g.r.running = True
    assert abs(g.imb()) < 5                                                 # looks 'fine' but both doubled
    g.r.ctx.in_session = lambda now=None: True
    g.move(9400, px)                                                         # clean set = -11.4%: not <=5
    acts = g.actions()
    assert any(a.startswith("re-centre") for a in acts), acts
    assert g.legs() == {"CE": {9400: 1, 9600: 1, 9800: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}
    assert abs(g.imb()) <= 15
    invariants(g)


def test_wide_atm_quote_does_not_freeze_the_book(universe):
    """GOLD 4-Nov: the new ATM PE stayed wide for hours, the engine waited and A never ran, so ITM CEs
    sat there at +26%. Now: the ATM rule still waits, but A rolls the ITM CEs out meanwhile."""
    g = Rig(universe, 9400)
    g.eng.start(N)
    n = len(g.r.book.trades)
    f = 9810                                                                # ATM 9400 -> 9800
    g.r.ctx.in_session = lambda now=None: False
    g.move(f)
    c = g.m.contract(N, g.e, 9800, "PE")
    g.m.apply([Tick(c.key, 150.0, 100.0, 200.0, 1, 1, 0, 0, 0, 0.0)])     # new ATM PE wide
    g.r.ctx.in_session = lambda now=None: True
    g.eng.on_ticks([Tick(g.fut.key, f, f - 1, f + 1, 1, 1, 0, 0, 0, 0.0)])
    lg = g.legs()
    assert 9800 not in lg["PE"]                                             # never sold into the wide quote
    assert "quote too wide" in (g.r.state["blocked"] or "") or any(
        e["event"] == "blocked" and "quote too wide" in e["why"] for e in g.r.events)
    assert len(g.r.book.trades) > n                                         # but it did keep adjusting
    assert not [k for k in lg["CE"] if k < 9800], f"ITM CE left: {lg}"      # ITM CEs rolled out
    assert sum(lg["CE"].values()) == sum(lg["PE"].values())
    _quote(g, 9800, "PE", 149.5, 150.5)                                     # fair quote -> ATM rule goes through
    assert g.legs()["PE"].get(9800)
    invariants(g)


def test_several_waiting_moves_log_once_each(universe):
    """4-Nov: ATM rule, re-centre and A all waiting on wide quotes took turns -> a log line every tick."""
    g = Rig(universe, 9400)
    g.eng.start(N)
    f = 9810
    g.r.ctx.in_session = lambda now=None: False
    g.move(f)
    for k, kind in ((9800, "PE"), (10000, "CE"), (10200, "CE"), (10400, "CE")):
        c = g.m.contract(N, g.e, k, kind)
        g.m.apply([Tick(c.key, 150.0, 100.0, 200.0, 1, 1, 0, 0, 0, 0.0)])
    g.r.ctx.in_session = lambda now=None: True
    g.eng.on_ticks([Tick(g.fut.key, f, f - 1, f + 1, 1, 1, 0, 0, 0, 0.0)])
    n = sum(1 for e in g.r.events if e["event"] == "blocked")
    for _ in range(20):
        g.eng.on_ticks([Tick(g.fut.key, f, f - 1, f + 1, 1, 1, 0, 0, 0, 0.0)])
    assert sum(1 for e in g.r.events if e["event"] == "blocked") == n


def test_history_records_six_strike_spreads(universe):
    import datetime as dt
    g = Rig(universe, 9400)
    g.eng.start(N)
    c = g.m.contract(N, g.e, 9600, "CE")
    g.m.apply([Tick(c.key, 150.0, 140.0, 160.0, 1, 1, 0, 0, 0, 0.0)])     # CE+1 gap 20 on mid 150
    g.eng.sample(dt.datetime.now())
    sp = g.eng.hist[N][-1]["sp"]
    assert len(sp) == 6 and sp[1] == round(20 / 150 * 100, 2)
    assert all(v is not None and v < 5 for j, v in enumerate(sp) if j != 1)
