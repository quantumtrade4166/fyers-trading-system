r"""
cepe_imbalance/test_flex.py — owner's 16/17-Nov FLEX rules (CePeFlex, NIFTY / SENSEX books) on the CRUDEOIL
toy chain (grid 200), offline.

  >= 2 strikes a side · heavy shift before B · flexible (partial) lots · <= 4 strikes a side · heavy front
  never past OTM2 · one side keeps ATM · no C, nothing SOLD in the money · net orders only

    .venv\Scripts\python.exe -m pytest live_trading_options/cepe_imbalance/test_flex.py -q
"""
import random

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import strategy as S
from live_trading_options.cepe_imbalance.engine import Runner
from live_trading_options.cepe_imbalance.test_strategy import N, Rig, universe  # noqa: F401  (fixture)

BAND = 15.0


def flex(g, lots=1):
    g.r.strategy = S.CePeFlex()
    g.r.strategy_name = "cepe_flex"
    g.r.params.update(lots=lots, band=BAND, step=200)
    return g


def hold(g, legs, atm):
    g.r.ctx.in_session = lambda now=None: False
    for kind, ks in legs.items():
        for k, lots in ks.items():
            g.r.book.order(g.m.contract(N, g.e, k, kind), "SELL", lots, "setup")
    g.r.state.update(phase="in", atm=atm)
    g.r.running = True
    g.r.ctx.in_session = lambda now=None: True


def sells(g, since=0):
    return [t for t in g.r.book.trades[since:] if t["side"] == "SELL"]


def check(g, f):
    """Hard rules after every step (spot f)."""
    lg = g.legs()
    atm = g.r.state.get("atm")
    assert sum(lg["CE"].values()) == sum(lg["PE"].values()), lg                    # lots equal
    for kind in ("CE", "PE"):
        assert len(lg[kind]) >= 2, f"{kind} on one strike: {lg}"
        assert len(lg[kind]) <= 4, f"{kind} on {len(lg[kind])} strikes: {lg}"
    assert atm in lg["CE"] or atm in lg["PE"], f"no side at ATM {atm}: {lg}"
    imb = g.imb()
    assert imb is None or abs(imb) <= BAND or g.r.state.get("blocked"), (imb, lg)


def test_books_pick_flex_rules(universe):
    from live_trading_options.cepe_imbalance.market import Market
    m = Market(universe)
    names = {n: Runner(n, m, persist=False).strategy_name for n in
             ("NIFTY", "NIFTY_ST", "SENSEX", "SENSEX_ST", "CRUDEOIL", "CRUDEOIL_ST", "BTC_NR", "BTC")}
    assert names == {"NIFTY": "cepe_flex", "NIFTY_ST": "cepe_st_flex", "SENSEX": "cepe_flex",
                     "SENSEX_ST": "cepe_st_flex", "CRUDEOIL": "cepe", "CRUDEOIL_ST": "cepe_st",
                     "BTC_NR": "cepe_new", "BTC": "cepe"}
    assert C.is_flex("NIFTY_LIVE") and C.is_flex("SENSEX_LIVE") and not C.is_flex("CRUDEOIL")


def test_no_C_in_flex():
    import inspect
    src = inspect.getsource(S.CePeFlex)
    assert "op_C(" not in src.split('"""', 2)[2]


def test_heavy_shift_comes_before_B(universe):
    """Market up: CE heavy -> the CE side is shifted OUT first, the PE side is left alone."""
    g = flex(Rig(universe, 9400))
    hold(g, {"CE": {9400: 1, 9600: 1, 9800: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}, 9400)
    n = len(g.r.book.trades)
    g.move(9520)                                                  # ATM stays 9400 (buffer), CE heavier
    assert g.imb() is not None
    acts = g.actions()
    lg = g.legs()
    if len(g.r.book.trades) > n:
        assert acts and acts[-1].startswith("flex"), acts
        assert "shift CE" in acts[-1], acts[-1]
        assert lg["PE"] == {9000: 1, 9200: 1, 9400: 1}, lg       # light side untouched: shift was enough
    check(g, 9520)


def test_light_side_never_on_one_strike(universe):
    """PE already on 2 strikes, CE heavy and no more heavy shift allowed: B may not put all PE on 1 strike."""
    px = {(9400, "CE"): 300.0, (9600, "CE"): 280.0, (9800, "CE"): 270.0, (10000, "CE"): 260.0,
          (10200, "CE"): 250.0, (9400, "PE"): 40.0, (9200, "PE"): 30.0}
    g = flex(Rig(universe, 9400, px))
    hold(g, {"CE": {9600: 1, 9800: 1, 10000: 1}, "PE": {9400: 2, 9200: 1}}, 9400)
    assert g.imb() > BAND
    g.move(9400, px)
    lg = g.legs()
    assert len(lg["PE"]) >= 2, lg
    assert g.r.state.get("blocked")                               # waits instead
    check(g, 9400)


def test_front_never_past_otm2(universe):
    """CE heavy, CE front already at OTM2: no further heavy shift - B instead."""
    px = {(9800, "CE"): 200.0, (10000, "CE"): 190.0, (10200, "CE"): 180.0, (10400, "CE"): 170.0,
          (9400, "PE"): 120.0, (9200, "PE"): 60.0, (9000, "PE"): 40.0}
    g = flex(Rig(universe, 9400, px))
    hold(g, {"CE": {9800: 1, 10000: 1, 10200: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}, 9400)
    assert g.imb() > BAND
    g.move(9400, px)
    lg = g.legs()
    assert min(lg["CE"]) <= 9800, lg                              # front stayed at OTM2
    acts = g.actions()
    assert acts and "B PE" in acts[-1], acts                      # then B on the light side
    assert "shift CE 9800" not in acts[-1], acts                  # the front strike itself never moved


def test_partial_lots_fewest_needed(universe):
    """4 lots a strike: a small breach moves only some lots, to a 4th strike."""
    g = flex(Rig(universe, 9400), lots=4)
    g.eng.start(N)
    assert g.legs()["CE"] == {9400: 4, 9600: 4, 9800: 4}
    g.move(9480)
    lg = g.legs()
    acts = [a for a in g.actions() if a.startswith("flex")]
    if acts:
        moved = sum(int(t["lots"]) for t in sells(g, 6))
        assert moved < 4, (acts, lg)                              # partial: fewer than a whole strike
        assert len(lg["CE"]) <= 4
    check(g, 9480)


def test_light_side_comes_back_to_atm_on_flip(universe):
    """CE shifted out (no CE at ATM), then the market falls: PE heavy -> CE comes straight back to ATM first."""
    g = flex(Rig(universe, 9400))
    hold(g, {"CE": {9600: 1, 9800: 1, 10000: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}, 9400)
    g.move(9330)
    lg = g.legs()
    acts = g.actions()
    if acts:
        assert 9400 in lg["CE"] or "back to normal" in acts[-1] or "re-centre" in acts[-1], (acts, lg)
    check(g, 9330)


def test_net_orders_only_and_no_itm_sells_on_random_walk(universe):
    rnd = random.Random(7)
    for lots in (1, 3):
        g = flex(Rig(universe, 9400), lots=lots)
        g.eng.start(N)
        f = 9400
        for _ in range(160):
            f += rnd.choice((-60, -30, 0, 30, 60))
            f = max(8600, min(10200, f))
            n = len(g.r.book.trades)
            g.move(f)
            atm = g.r.state.get("atm")
            new = g.r.book.trades[n:]
            for t in new:
                if t["side"] == "SELL":
                    k, kind = float(t["strike"]), t["kind"]
                    assert (k >= atm) if kind == "CE" else (k <= atm), f"ITM sell {kind} {k} atm {atm}"
            keys = {}
            for e in [e for e in g.r.events if e["event"] == "adjust"][-1:]:
                for fl in e.get("fills") or []:
                    side, _n, sym = fl.split()[:3]
                    keys.setdefault(sym, set()).add(side)
            assert all(len(v) == 1 for v in keys.values()), keys     # no strike bought AND sold in one move
            check(g, f)


def test_stale_index_price_holds_adjustments(universe):
    import time
    from live_trading_options.cepe_imbalance.feed import Tick
    g = flex(Rig(universe, 9400))
    hold(g, {"CE": {9400: 1, 9600: 1, 9800: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}, 9400)
    g.move(9560, run=False)                                       # CE heavy beyond the band
    old = time.time() - 30
    g.m.apply([Tick(g.fut.key, 9560, 9559, 9561, 1, 1, 0, 0, 0, old)])
    assert g.imb() > BAND
    n = len(g.r.book.trades)
    c = g.m.contract(N, g.e, 9400, "CE")
    q = g.m.quote(c.key)
    g.eng.on_ticks([Tick(c.key, q.ltp, q.bid, q.ask, 1, 1, 0, 0, 0, 0.0)])
    assert len(g.r.book.trades) == n                              # nothing traded on a stale index price
    assert "prices stale" in (g.r.state.get("blocked") or "")
    g.move(9560)                                                  # fresh ticks again -> it adjusts
    assert len(g.r.book.trades) > n


# ── 17-Nov: paired entry, resize in trade, auto-start to the second ─────────
def _sides_equal_after_each_pair(trades):
    ce = pe = 0
    for i, t in enumerate(trades):
        if t["kind"] == "CE":
            ce += int(t["lots"])
        else:
            pe += int(t["lots"])
        if i % 2 == 1:
            assert ce == pe, f"after order {i + 1}: CE {ce} vs PE {pe}"


def test_entry_goes_out_in_ce_pe_pairs(universe):
    g = flex(Rig(universe, 9400), lots=4)
    g.eng.start(N)
    tr = g.r.book.trades
    assert [t["kind"] for t in tr[:2]] == ["CE", "PE"]
    _sides_equal_after_each_pair(tr)
    assert {t["side"] for t in tr} == {"SELL"}


def test_paired_splits_uneven_lots():
    ops = [("CE", 100, "SELL", 4), ("CE", 110, "SELL", 4), ("PE", 100, "SELL", 3), ("PE", 90, "SELL", 5)]
    out = S.CePeImbalance.paired(ops, atm=100)
    assert out[:2] == [("CE", 100, "SELL", 3), ("PE", 100, "SELL", 3)]          # nearest the money first
    for i in range(0, len(out), 2):
        assert out[i][0] == "CE" and out[i + 1][0] == "PE" and out[i][3] == out[i + 1][3]
    assert sum(o[3] for o in out if o[0] == "CE") == 8 == sum(o[3] for o in out if o[0] == "PE")


def test_lots_cut_in_trade_buys_back_every_strike(universe):
    g = flex(Rig(universe, 9400), lots=4)
    g.eng.start(N)
    before = g.legs()
    n = len(g.r.book.trades)
    res = g.eng.set_params(N, {"lots": 2})
    assert res["ok"] and "4 -> 2" in res["resize"]
    g.move(9400)
    lg = g.legs()
    assert sum(lg["CE"].values()) == sum(lg["PE"].values()) == 6
    assert all(lg[k][s] == before[k][s] // 2 for k in lg for s in lg[k])          # same shape, halved
    new = g.r.book.trades[n:]
    assert new and {t["side"] for t in new} == {"BUY"}                            # a cut only buys back
    _sides_equal_after_each_pair(new)
    assert not g.r.state.get("resize_to")
    assert any(e["event"] == "adjust" and e["action"].startswith("resize 4 -> 2") for e in g.r.events)


def test_lots_raise_in_trade_adds_on_every_strike(universe):
    g = flex(Rig(universe, 9400), lots=2)
    g.eng.start(N)
    n = len(g.r.book.trades)
    g.eng.set_params(N, {"lots": 3})
    g.move(9400)
    lg = g.legs()
    assert sum(lg["CE"].values()) == sum(lg["PE"].values()) == 9
    new = g.r.book.trades[n:]
    assert new and {t["side"] for t in new} == {"SELL"}
    _sides_equal_after_each_pair(new)


def test_resize_uneven_shape_keeps_sides_equal(universe):
    g = flex(Rig(universe, 9400), lots=4)
    hold(g, {"CE": {9400: 3, 9600: 4, 9800: 5}, "PE": {9000: 5, 9200: 4, 9400: 3}}, 9400)
    g.eng.set_params(N, {"lots": 2})
    g.move(9400)
    lg = g.legs()
    assert sum(lg["CE"].values()) == sum(lg["PE"].values()) == 6, lg
    assert all(v <= {9400: 3, 9600: 4, 9800: 5}.get(s, 0) for s, v in lg["CE"].items())


def test_lots_change_when_flat_only_sets_next_entry(universe):
    g = flex(Rig(universe, 9400), lots=1)
    res = g.eng.set_params(N, {"lots": 3})
    assert res["ok"] and "resize" not in res and not g.r.state.get("resize_to")


def test_auto_time_to_the_second(universe):
    g = flex(Rig(universe, 9400))
    assert g.eng.set_auto(N, True, "09:15:20")["auto_time"] == "09:15:20"
    assert g.eng.set_auto(N, True, "09:16")["auto_time"] == "09:16"
    import datetime as dt
    from live_trading_options.cepe_imbalance.book import IST
    g.r.running = False
    g.r.state.pop("auto_done", None)
    g.eng.set_auto(N, True, "10:00:30")
    day = dt.date(2026, 11, 3)
    g.eng.maybe_auto_start(N, g.r, dt.datetime.combine(day, dt.time(10, 0, 29), IST))
    assert not g.r.running
    g.eng.maybe_auto_start(N, g.r, dt.datetime.combine(day, dt.time(10, 0, 30), IST))
    assert g.r.running


def test_exit_when_atm_ce_pe_above_level(universe):
    g = flex(Rig(universe, 9400))
    g.eng.start(N)
    cp = g.r.strategy.atm_straddle(g.r.ctx, 9400)
    assert cp and cp > 0
    g.eng.set_params(N, {"exit_above": round(cp + 40, 1)})
    g.move(9400)
    assert g.r.state["phase"] == "in"                               # level not reached
    px = {(9400, "CE"): cp / 2 + 25, (9400, "PE"): cp / 2 + 25}     # CE+PE rises 50
    g.move(9400, px)
    assert g.r.state["phase"] == "stopped" and not any(p.lots for p in g.r.book.positions.values())
    assert any(e["event"] == "level_exit" and ">=" in e["why"] for e in g.r.events)
    n = len(g.r.book.trades)
    g.move(9400)
    assert len(g.r.book.trades) == n                                 # stopped for the day: no re-entry


def test_exit_when_atm_ce_pe_below_level_and_off_by_default(universe):
    g = flex(Rig(universe, 9400))
    g.eng.start(N)
    cp = g.r.strategy.atm_straddle(g.r.ctx, 9400)
    px = {(9400, "CE"): cp / 2 - 30, (9400, "PE"): cp / 2 - 30}
    g.move(9400, px)
    assert g.r.state["phase"] == "in"                               # 0 = off: nothing happens
    g.eng.set_params(N, {"exit_below": round(cp - 40, 1)})
    g.move(9400, px)
    assert g.r.state["phase"] == "stopped" and not any(p.lots for p in g.r.book.positions.values())
    assert "<=" in g.r.state["blocked"]


def test_stop_exit_marks_flat_and_chart_follows_market_atm(universe):
    g = flex(Rig(universe, 9400))
    g.eng.start(N)
    g.eng.stop(N, exit_positions=True)
    assert g.r.state["phase"] == "exited"
    g.move(9800, run=False)                                       # market moves 2 strikes; book stopped
    import datetime as dt
    g.eng.sample(dt.datetime.now())
    row = g.eng.hist[N][-1]
    assert row["atm"] == g.m.atm(N, g.e) != 9400                  # chart follows the market, not frozen


def test_heavy_side_never_walks_back_strikes_far_out(universe):
    """17-Nov paper NIFTY: CE front stayed at OTM1 while 1-lot back legs were shifted 25550 -> ... -> 26000 in
    tiny steps. The heavy side may span at most 4 strikes (front .. front+3) and each step must gain 0.5 pt."""
    px = {(9400, "CE"): 400.0, (9600, "CE"): 30.0, (9800, "CE"): 25.0, (10000, "CE"): 20.0, (10200, "CE"): 15.0,
          (10400, "CE"): 10.0, (10600, "CE"): 6.0, (10800, "CE"): 3.0, (11000, "CE"): 1.0,
          (9400, "PE"): 120.0, (9200, "PE"): 60.0, (9000, "PE"): 40.0}
    g = flex(Rig(universe, 9400, px))
    hold(g, {"CE": {9400: 1, 9600: 1, 9800: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}, 9400)
    for _ in range(5):
        g.move(9400, px)
        lg = g.legs()
        ks = sorted(lg["CE"])
        assert ks[-1] - ks[0] <= 3 * 200, lg                        # never beyond the 4th strike
    for a in g.actions():
        for part in a.split("; "):
            if "shift" in part:
                assert "10400" not in part and "10600" not in part, a
