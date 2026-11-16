r"""
cepe_imbalance/test_new_rules.py — owner's 16-Nov new rules (CePeNewRules) on the CRUDEOIL toy chain, offline.

  no C (never sell ITM) · entry/re-centre balance by rolling the heavy side OUT · wait for A (no B fall-through)
  · A only when it lowers |imbalance| · every side on >= 2 strikes

    .venv\Scripts\python.exe -m pytest live_trading_options/cepe_imbalance/test_new_rules.py -q
"""
from live_trading_options.cepe_imbalance import strategy as S
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.test_strategy import N, Rig, universe  # noqa: F401  (fixture)

P = {"band": 30.0, "step": 200}


def nr(g):
    g.r.strategy = S.CePeNewRules()
    g.r.strategy_name = "cepe_new"
    g.r.params.update(P)
    return g


def hold(g, legs, atm):
    g.r.ctx.in_session = lambda now=None: False
    for kind, ks in legs.items():
        for k, lots in ks.items():
            g.r.book.order(g.m.contract(N, g.e, k, kind), "SELL", lots, "setup")
    g.r.state.update(phase="in", atm=atm)
    g.r.running = True
    g.r.ctx.in_session = lambda now=None: True


def itm(lg, f):
    return [("CE", k) for k in lg["CE"] if k < f] + [("PE", k) for k in lg["PE"] if k > f]


def test_expiry_entry_rolls_heavy_side_out_no_itm(universe):
    """ATM CE 30 vs ATM PE 150 (spot just under the ATM strike): PE side made lighter, nothing ITM."""
    exp = {(9400, "CE"): 40.0, (9600, "CE"): 35.0, (9800, "CE"): 34.0,          # (+-0.5 quotes: keep >= 34)
           (9400, "PE"): 150.0, (9200, "PE"): 40.0, (9000, "PE"): 35.0, (8800, "PE"): 34.0, (8600, "PE"): 34.0,
           (9200, "CE"): 160.0}
    g = nr(Rig(universe, 9380, exp))
    g.eng.start(N)
    lg = g.legs()
    assert all(t["side"] == "SELL" for t in g.r.book.trades)
    assert lg["CE"] == {9400: 1, 9600: 1, 9800: 1}                         # light side untouched, no ITM CE
    assert lg["PE"] == {8800: 1, 9000: 1, 9200: 1}                         # ATM PE rolled out behind the last
    assert abs(g.imb()) <= 30
    assert not any(e["event"] == "adjust" and e["action"].startswith("C") for e in g.r.events)
    assert any("pre-A PE 9400->8800" in (e.get("action") or "") for e in g.r.events)


def test_no_C_in_new_rules():
    import inspect
    src = inspect.getsource(S.CePeNewRules)
    assert "op_C" not in src.split('"""', 2)[2]                            # C is never built by the new rules


def test_wait_for_A_on_wide_quote_never_B(universe):
    g = nr(Rig(universe, 9400))
    hold(g, {"CE": {9400: 1, 9600: 1, 9800: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}, 9400)
    c = g.m.contract(N, g.e, 10000, "CE")                                  # A's target strike quote wide
    g.m.apply([Tick(c.key, 30.0, 10.0, 50.0, 1, 1, 0, 0, 0, 0.0)])
    f = 9640                                                               # CE heavy beyond 30 %
    g.move(f, run=False)
    g.m.apply([Tick(c.key, 30.0, 10.0, 50.0, 1, 1, 0, 0, 0, 0.0)])
    assert g.imb() > 30
    n = len(g.r.book.trades)
    g.eng.on_ticks([Tick(g.fut.key, f, f - 1, f + 1, 1, 1, 0, 0, 0, 0.0)])
    assert len(g.r.book.trades) == n                                       # nothing traded: waiting for A
    assert "quote too wide" in (g.r.state.get("blocked") or "")
    g.move(f)                                                              # fair quote again -> A goes
    lg = g.legs()
    assert 10000 in lg["CE"]
    acts = [e["action"] for e in g.r.events if e["event"] == "adjust"]
    assert acts and not any(a.startswith(("B", "C")) for a in acts), acts    # A / re-centre, never B or C


def test_A_rolls_front_including_atm_and_stays_two_strikes(universe):
    g = nr(Rig(universe, 9400))
    hold(g, {"CE": {9400: 1, 9600: 1, 9800: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}, 9400)
    g.move(9700)
    lg = g.legs()
    assert len(lg["CE"]) >= 2 and len(lg["PE"]) >= 2
    assert sum(lg["CE"].values()) == sum(lg["PE"].values()) == 3
    assert abs(g.imb()) <= 30 or g.r.state.get("blocked")
    assert not [k for k in lg["PE"] if k > 9700], lg                       # no PE ITM (spot 9700)


def test_strikes_ok_blocks_one_strike_side():
    st = S.CePeNewRules()
    old = {"CE": {1: 1, 2: 1, 3: 1}, "PE": {5: 1, 4: 2}}
    assert not st.strikes_ok(old, {"CE": old["CE"], "PE": {4: 3}})         # 3 lots on 1 strike: no
    assert st.strikes_ok(old, {"CE": old["CE"], "PE": {4: 2, 3: 1}})
    assert st.strikes_ok({"CE": {1: 3}, "PE": {5: 3}}, {"CE": {1: 2, 9: 1}, "PE": {5: 3}})   # A spreads it out


def test_A_skipped_when_it_would_not_help(universe):
    """Heavy CE +35 % but rolling CE out flips it to a worse PE-heavy book: wait, no B either."""
    px = {(9400, "CE"): 160.0, (9600, "CE"): 10.0, (9800, "CE"): 10.0, (10000, "CE"): 1.0,
          (9400, "PE"): 30.0, (9200, "PE"): 25.0, (9000, "PE"): 25.0}
    g = nr(Rig(universe, 9400, px))
    hold(g, {"CE": {9400: 1, 9600: 1, 9800: 1}, "PE": {9000: 1, 9200: 1, 9400: 1}}, 9400)
    assert g.imb() > 30
    n = len(g.r.book.trades)
    g.eng.on_ticks([Tick(g.fut.key, 9400, 9399, 9401, 1, 1, 0, 0, 0, 0.0)])
    assert len(g.r.book.trades) == n
    assert "would not lower" in (g.r.state.get("blocked") or "")
