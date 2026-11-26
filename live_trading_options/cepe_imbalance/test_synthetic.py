r"""
cepe_imbalance/test_synthetic.py — owner's SYNTHETIC TO-AND-FRO rules (synthetic.py), offline, NIFTY toy chain.

    .venv\Scripts\python.exe -m pytest live_trading_options/cepe_imbalance/test_synthetic.py -q
"""
import datetime as dt
import math

import pytest

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance.engine import Engine, Runner
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.market import Market
from live_trading_options.cepe_imbalance.strategy import CePeImbalance
from live_trading_options.cepe_imbalance.test_strategy import universe  # noqa: F401  (fixture)

N = "NIFTY_SYN"


def model(k, kind, f):
    """Put-call parity holds exactly: CE - PE = f - k, so the synthetic = f."""
    intr = max(f - k, 0) if kind == "CE" else max(k - f, 0)
    return round(intr + 60 * math.exp(-((k - f) / 300) ** 2) + 5, 2)


class Rig:
    def __init__(self, universe, f, name=N, spot=None):
        self.m = Market(universe)
        self.eng = Engine(universe, self.m, persist=False)
        self.r = self.eng.runners[name]
        self.r.ctx.in_session = lambda now=None: True
        self.r.params["exit_time"] = ""                   # tests run at any clock time
        self.und = C.und_of(name)
        self.e = self.m.expiry(self.und)
        self.spot_key = universe.underlyings[self.und].spot_key
        self.move(f, spot=spot, run=False)
        self.eng.refresh_subscriptions()

    def move(self, f, spot=None, run=True, override=None):
        override = override or {}
        ticks = [Tick(self.spot_key, spot if spot is not None else f, 0, 0, 0, 0, 0, 0, 0, 0.0)]
        step = C.STRIKE_STEP[self.und]
        lo, hi = int(f - 30 * step), int(f + 30 * step)
        for k in range(lo - lo % step, hi, step):
            for kind in ("CE", "PE"):
                c = self.m.contract(self.und, self.e, k, kind)
                if c:
                    px = override.get((k, kind), model(k, kind, f))
                    ticks.append(Tick(c.key, px, px - 0.05, px + 0.05, 1, 1, 0, 0, 0, 0.0))
        if run:
            self.eng.on_ticks(ticks)
        else:
            self.m.apply(ticks)

    def start(self):
        out = self.eng.start(self.r.name)
        assert out.get("ok", True), out
        return out

    def legs(self):
        lg = CePeImbalance.legs(self.r.ctx)
        return {k: sorted(v) for k, v in lg.items()}

    def lots(self):
        return CePeImbalance.legs(self.r.ctx)

    def actions(self):
        return [e["action"] for e in self.r.events if e["event"] == "adjust" and not e["action"].startswith("entry")]


def test_books_and_strategy(universe):
    m = Market(universe)
    for n in ("NIFTY_SYN", "SENSEX_SYN", "NIFTY_SYN_LIVE", "SENSEX_SYN_LIVE"):
        assert Runner(n, m, persist=False).strategy_name == "syn_tofro", n
        assert C.is_syn(n) and C.und_of(n) in ("NIFTY", "SENSEX") and not C.is_flex(n)
    assert Runner("NIFTY", m, persist=False).strategy_name == "cepe_flex"       # CE/PE books untouched
    assert Runner("NIFTY_LIVE", m, persist=False).strategy_name == "cepe_flex"
    assert C.book_in_role("NIFTY_SYN", "paper") and not C.book_in_role("NIFTY_SYN", "live")
    assert C.book_in_role("NIFTY_SYN_LIVE", "live") and not C.book_in_role("NIFTY_SYN_LIVE", "paper")


def test_synthetic_price(universe):
    g = Rig(universe, 25012, spot=25040)                  # spot off; options say 25012
    syn, k = g.r.strategy.synthetic(g.r.ctx)
    assert abs(syn - 25012) < 0.01 and k == 25000


def test_owner_example_up_and_back(universe):
    """Owner 23-Nov: 25000 -> 25030 call out, 25055 put in, then back down = the starting book."""
    g = Rig(universe, 25000)
    g.start()
    assert g.legs() == {"CE": [25000, 25050, 25100], "PE": [24900, 24950, 25000]}
    g.move(25029)
    assert g.legs()["CE"] == [25000, 25050, 25100]        # 29 < 25 + 5: nothing yet
    g.move(25030)
    assert g.legs() == {"CE": [25050, 25100, 25150], "PE": [24900, 24950, 25000]}, g.actions()
    assert g.actions()[-1].startswith("call out")
    g.move(25054)
    assert g.legs()["PE"] == [24900, 24950, 25000]
    g.move(25055)
    assert g.legs() == {"CE": [25050, 25100, 25150], "PE": [24950, 25000, 25050]}, g.actions()
    assert g.actions()[-1].startswith("put in")
    # reversal (mirror from the current book)
    g.move(25020)
    assert g.legs() == {"CE": [25050, 25100, 25150], "PE": [24900, 24950, 25000]}, g.actions()
    assert g.actions()[-1].startswith("put out")
    g.move(24995)
    assert g.legs() == {"CE": [25000, 25050, 25100], "PE": [24900, 24950, 25000]}, g.actions()
    assert g.actions()[-1].startswith("call in")
    assert len(g.actions()) == 4


def test_keeps_going_up(universe):
    g = Rig(universe, 25000)
    g.start()
    for f, want in ((25080, {"CE": [25100, 25150, 25200], "PE": [24950, 25000, 25050]}),
                    (25105, {"CE": [25100, 25150, 25200], "PE": [25000, 25050, 25100]})):
        g.move(f)
        assert g.legs() == want, (f, g.actions())


def test_down_mirror(universe):
    g = Rig(universe, 25000)
    g.start()
    g.move(24970)
    assert g.legs() == {"CE": [25000, 25050, 25100], "PE": [24850, 24900, 24950]}, g.actions()
    g.move(24945)
    assert g.legs() == {"CE": [24950, 25000, 25050], "PE": [24850, 24900, 24950]}, g.actions()


def test_big_jump_repeats_steps(universe):
    """A 160-pt jump: every trigger crossed is worked off one strike at a time, outs before ins."""
    g = Rig(universe, 25000)
    g.start()
    g.move(25160)
    lg = g.legs()
    # calls: 25000 out (>=25030), 25050 out (>=25080), 25100 out (>=25130); next 25150 needs 25180
    assert lg["CE"] == [25150, 25200, 25250], g.actions()
    # puts: in at 25055 (->25050), 25105 (->25100), 25155 (->25150); next needs 25205
    assert lg["PE"] == [25050, 25100, 25150], g.actions()
    lots = g.lots()
    assert sum(lots["CE"].values()) == sum(lots["PE"].values()) == 3


def test_no_ping_pong_inside_hysteresis(universe):
    g = Rig(universe, 25000)
    g.start()
    for f in (25010, 24990, 25025, 24975, 25000, 25028, 24972):
        g.move(f)
    assert g.actions() == [], g.actions()


def test_lots_and_buffer_editable(universe):
    g = Rig(universe, 25000)
    g.eng.set_params(N, {"lots": 2, "buffer": 10})
    g.start()
    assert all(v == 2 for side in g.lots().values() for v in side.values())
    g.move(25030)
    assert g.actions() == []                              # 25 + 10 = 35 now
    g.move(25035)
    assert g.legs()["CE"] == [25050, 25100, 25150] and g.lots()["CE"][25150] == 2


def test_sensex_uses_100_grid(universe):
    k0 = 78000
    g = Rig(universe, k0, name="SENSEX_SYN")
    g.start()
    assert g.legs() == {"CE": [k0, k0 + 100, k0 + 200], "PE": [k0 - 200, k0 - 100, k0]}
    g.move(k0 + 54)
    assert g.actions() == []
    g.move(k0 + 55)                                       # 50 + 5
    assert g.legs()["CE"] == [k0 + 100, k0 + 200, k0 + 300]
    g.move(k0 + 105)                                      # 100 + 5
    assert g.legs()["PE"] == [k0 - 100, k0, k0 + 100]


def test_exit_time(universe):
    g = Rig(universe, 25000)
    g.start()
    g.r.params["exit_time"] = "00:00"                     # already past
    g.move(25001)
    assert g.r.state["phase"] == "done"
    assert not any(p.lots for p in g.r.book.positions.values())


def test_mtm_stop(universe):
    g = Rig(universe, 25000)
    g.r.params["mtm_stop"] = 500.0
    g.start()
    g.move(25000, override={(25000, "CE"): 200.0})        # one CE spikes: loss > 500 on 65 qty
    assert g.r.state["phase"] == "stopped"


def test_waits_without_synthetic(universe):
    g = Rig(universe, 25000)
    g.m.quotes.pop(g.m.contract("NIFTY", g.e, 25000, "PE").key)
    g.start()
    assert not g.r.book.trades
    assert "synthetic" in (g.r.state.get("blocked") or "")


def test_view(universe):
    g = Rig(universe, 25000)
    g.start()
    v = g.eng.summary()["underlyings"]
    row = next(u for u in v if u["name"] == N)
    assert row["variant"] == "syn"
    assert row["syn"]["levels"] == {"call_out": 25030, "call_in": 24945, "put_in": 25055, "put_out": 24970}
    assert row["imbalance"]["pct"] is not None            # shown for viewing only


# ── LIVE book (fake Kotak, offline) ─────────────────────────────────────────
def test_live_book_rolls_with_real_orders(universe):
    from live_trading_options.cepe_imbalance import test_live as TL
    TL.LV.WAIT_FIRST_S = TL.LV.WAIT_RETRY_S = TL.LV.CONFIRM_S = 0.05
    TL.LV.POLL_S = TL.LV.POS_AFTER_FILL_S = TL.LV.EXIT_RETRY_S = 0.0
    g = TL.Rig(universe)
    name = "NIFTY_SYN_LIVE"
    g.r = g.eng.runners[name]
    g.r.ctx.in_session = lambda now=None: True
    g.r.params["exit_time"] = ""
    assert g.r.live and g.r.strategy_name == "syn_tofro"
    assert g.r.params["lots"] == 1 and g.r.params["mtm_stop"] == 5000      # live defaults
    assert not g.eng.start(name)["ok"]                                      # not armed -> refused
    g.ctx.control.set(name, True)
    assert g.eng.start(name)["ok"]
    assert g.legs() == {"CE": {25600: 1, 25650: 1, 25700: 1}, "PE": {25500: 1, 25550: 1, 25600: 1}}
    n = len(g.k.orders)
    g.move(25650)              # call out (trigger 25630; the test_live rig only re-prices whole 50s)
    assert g.legs()["CE"] == {25650: 1, 25700: 1, 25750: 1}
    placed = [kw for t, kw in g.k.calls if t == "place"][-2:]
    assert [kw["transaction_type"] for kw in placed] == ["B", "S"]          # buy back first, then sell
    assert len(g.k.orders) == n + 2
    assert g.broker_net() == {k: -65 for k, p in g.r.book.positions.items() if p.lots}


# ── V2: option-price triggers (owner 25-Nov-2026) ───────────────────────────
def v2(g):
    from live_trading_options.cepe_imbalance.synthetic import SynToFroV2
    g.r.strategy, g.r.strategy_name = SynToFroV2(), "syn_tofro_v2"
    g.r.params = {**SynToFroV2.params, "exit_time": ""}
    return g


def test_v2_up_and_back(universe):
    """Toy prices: CE(k+50) = PE(k) exactly at k+25, CE(k) = PE(k) at k. So V2 fires at the half-way / ATM
    points (no buffer): call out 25025, put in 25050, back down put out 25025->..., call in at the ATM."""
    g = v2(Rig(universe, 25000))
    g.start()
    assert g.legs() == {"CE": [25000, 25050, 25100], "PE": [24900, 24950, 25000]}
    g.move(25024)
    assert g.actions() == []                                   # 25050 CE still cheaper than 25000 PE
    g.move(25026)
    assert g.legs() == {"CE": [25050, 25100, 25150], "PE": [24900, 24950, 25000]}, g.actions()
    assert g.actions()[-1].startswith("call out")
    g.move(25049)
    assert g.legs()["PE"] == [24900, 24950, 25000]             # 25050 PE still dearer than 25050 CE
    g.move(25051)
    assert g.legs() == {"CE": [25050, 25100, 25150], "PE": [24950, 25000, 25050]}, g.actions()
    assert g.actions()[-1].startswith("put in")
    g.move(25074)
    assert len(g.actions()) == 2                               # next call out only at 25075
    g.move(25024)                                              # PE 25000 >= CE 25050 -> put out
    assert g.legs()["PE"] == [24900, 24950, 25000], g.actions()
    assert g.actions()[-1].startswith("put out")
    g.move(25001)
    assert g.legs()["CE"] == [25050, 25100, 25150]
    g.move(24999)                                              # CE 25000 <= PE 25000 -> call in
    assert g.legs() == {"CE": [25000, 25050, 25100], "PE": [24900, 24950, 25000]}, g.actions()
    assert g.actions()[-1].startswith("call in")
    assert len(g.actions()) == 4


def test_v2_down_mirror_and_no_early_call_in(universe):
    """Falling from the start: put out at 24975 (half way), call in only once 24950 is the ATM."""
    g = v2(Rig(universe, 25000))
    g.start()
    g.move(24974)
    assert g.legs() == {"CE": [25000, 25050, 25100], "PE": [24850, 24900, 24950]}, g.actions()
    assert [a.split(" (")[0] for a in g.actions()] == ["put out"]
    g.move(24951)
    assert len(g.actions()) == 1
    g.move(24949)
    assert g.legs() == {"CE": [24950, 25000, 25050], "PE": [24850, 24900, 24950]}, g.actions()
    g.move(24924)                                              # next put out half way below 24950
    assert g.legs()["PE"] == [24800, 24850, 24900], g.actions()


def test_v2_waits_without_ltp(universe):
    g = v2(Rig(universe, 25000))
    g.start()
    g.m.quotes.pop(g.m.contract("NIFTY", g.e, 25050, "CE").key)
    g.r.strategy.evaluate(g.r.ctx)
    assert "V2 waiting for LTP" in (g.r.state.get("blocked") or "")


# ── Premium (V2) books, live (owner 25-Nov-2026) ────────────────────────────
def test_premium_books_use_v2(universe):
    m = Market(universe)
    for n in ("NIFTY_SYN2", "SENSEX_SYN2", "NIFTY_SYN2_LIVE", "SENSEX_SYN2_LIVE"):
        assert Runner(n, m, persist=False).strategy_name == "syn_tofro_v2", n
        assert C.syn_version(n) == 2 and C.und_of(n) in ("NIFTY", "SENSEX") and "Premium" in C.label_of(n)
    assert Runner("NIFTY_SYN", m, persist=False).strategy_name == "syn_tofro"


def _live_rig(universe, name):
    from live_trading_options.cepe_imbalance import test_live as TL
    TL.LV.WAIT_FIRST_S = TL.LV.WAIT_RETRY_S = TL.LV.CONFIRM_S = 0.05
    TL.LV.POLL_S = TL.LV.POS_AFTER_FILL_S = TL.LV.EXIT_RETRY_S = 0.0
    g = TL.Rig(universe)
    g.r = g.eng.runners[name]
    g.r.ctx.in_session = lambda now=None: True
    g.r.params["exit_time"] = ""
    g.ctx.control.set(name, True)
    return g


def test_premium_live_rolls_on_option_prices(universe):
    g = _live_rig(universe, "NIFTY_SYN2_LIVE")
    assert g.eng.start("NIFTY_SYN2_LIVE")["ok"]
    assert g.legs() == {"CE": {25600: 1, 25650: 1, 25700: 1}, "PE": {25500: 1, 25550: 1, 25600: 1}}
    g.move(25650)                         # 25650 CE >= 25600 PE -> call out (fires half way, test rig moves in 50s)
    lg = g.legs()
    assert 25600 not in lg["CE"] and len(lg["CE"]) == 3, lg
    placed = [kw for t, kw in g.k.calls if t == "place"]
    assert [kw["transaction_type"] for kw in placed[6:8]] == ["B", "S"]     # buy back first, then sell
    assert g.broker_net() == {k: -65 for k, p in g.r.book.positions.items() if p.lots}


def test_lots_change_in_trade_resizes_live_book(universe):
    """Owner: lots editable in the live market - 1 -> 2 lots adds on every strike (CE/PE pairs), 2 -> 1 buys back."""
    g = _live_rig(universe, "NIFTY_SYN_LIVE")
    g.eng.start("NIFTY_SYN_LIVE")
    out = g.eng.set_params("NIFTY_SYN_LIVE", {"lots": 2})
    assert out["ok"] and out.get("resize"), out
    g.r.strategy.evaluate(g.r.ctx)
    assert all(v == 2 for side in g.legs().values() for v in side.values()), g.legs()
    assert g.broker_net() == {k: -130 for k, p in g.r.book.positions.items() if p.lots}
    g.eng.set_params("NIFTY_SYN_LIVE", {"lots": 1})
    g.r.strategy.evaluate(g.r.ctx)
    assert all(v == 1 for side in g.legs().values() for v in side.values()), g.legs()
    g.move(25650)                         # rolls after a resize move the whole strike (1 lot)
    assert sum(g.legs()["CE"].values()) == sum(g.legs()["PE"].values()) == 3


# ── 26-Nov-2026: entry fixes (stale opening prices, slow fills, market moving during the entry) ──────────────

def test_entry_waits_until_synthetic_agrees_with_index(universe):
    """09:15:10 SENSEX: option LTPs still said 83,932 while the index was 84,030 -> sold the wrong ATM."""
    g = Rig(universe, 25000, spot=25120)                 # options stale (25000), index already 25120
    g.start()
    assert g.legs() == {"CE": [], "PE": []}
    assert "entry: waiting" in (g.r.state.get("blocked") or ""), g.r.state.get("blocked")
    g.move(25120)                                        # option prices catch up
    assert g.legs() == {"CE": [25100, 25150, 25200], "PE": [25000, 25050, 25100]}, g.r.events[-3:]


def test_entry_recentres_when_the_market_moves_mid_entry(universe):
    """Remaining entry legs go around the NEW ATM if the market moved a strike while earlier pairs filled."""
    g = Rig(universe, 25000)
    st = g.r.strategy
    real = st.execute
    calls = []

    def execute(ctx, ops, reason):
        ok = real(ctx, ops, reason)
        calls.append(reason)
        if len(calls) == 1:                              # market runs 100 points while pair 1 fills
            g.move(25100, run=False)
        return ok
    st.execute = execute
    g.start()
    assert g.legs() == {"CE": [25000, 25150, 25200], "PE": [24950, 25000, 25050]}, (calls, g.legs())
    ev = [e for e in g.r.events if e["event"] == "entry_recentre"]
    assert ev and ev[0]["frm"] == 25000 and ev[0]["to"] == 25100
    assert g.r.state["phase"] == "in" and not g.r.state.get("entry_left")


def test_interrupted_entry_finishes_on_the_next_tick(universe):
    g = Rig(universe, 25000)
    st = g.r.strategy
    real = st.execute
    n = {"c": 0}

    def execute(ctx, ops, reason):
        n["c"] += 1
        if n["c"] == 2:                                  # pair 2 refused once (quote too wide, say)
            return False
        return real(ctx, ops, reason)
    st.execute = execute
    g.start()
    assert g.r.state["phase"] == "in" and g.r.state.get("entry_left"), g.r.state
    assert sum(len(v) for v in g.legs().values()) == 2
    g.move(25000)
    assert g.legs() == {"CE": [25000, 25050, 25100], "PE": [24900, 24950, 25000]}, g.legs()
    assert not g.r.state.get("entry_left")


def test_synthetic_books_ask_for_fast_orders(universe):
    g = Rig(universe, 25000)
    seen = []
    real = g.r.book.order

    def order(c, side, lots, reason="", **kw):
        seen.append(kw)
        return real(c, side, lots, reason, **kw)
    g.r.book.order = order
    g.start()
    assert seen and all(k.get("fast") for k in seen)
