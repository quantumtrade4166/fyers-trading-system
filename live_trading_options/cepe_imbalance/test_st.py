"""
cepe_imbalance/test_st.py — Supertrend variant (owner 13-Nov-2026): the indicator, the signal state
(opening rule, flips), the variant book's enter / exit, and the warm-up replay from recorded quotes.
"""
import datetime as dt
import random

import pytest

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import quotes as Q
from live_trading_options.cepe_imbalance import st_signal as STS
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.market import Market
from live_trading_options.cepe_imbalance.test_strategy import N, Rig, model, universe  # noqa: F401

IST = STS.IST


# ── indicator ───────────────────────────────────────────────────────────────
def _lcg_bars():
    s = 7

    def rnd():
        nonlocal s
        s = (s * 16807) % 2147483647
        return s / 2147483647
    c, bars = 100.0, []
    for i in range(300):
        o = c
        c = max(5, c + (rnd() - 0.5) * 6)
        bars.append({"t": i * 60, "o": o, "h": max(o, c) + rnd() * 2, "l": min(o, c) - rnd() * 2, "c": c})
    return bars


def test_supertrend_matches_tradingview_reference():
    """Same random bars + values as the independent Pine-logic check done for the chart (13-Nov)."""
    st = STS.supertrend(_lcg_bars(), 10, 3)
    assert len(st) == 291
    assert (st[0][1], st[0][2]) == (115.01, False)                 # starts in a down trend, like TradingView
    assert (st[66][1], st[66][2]) == (103.99, False)
    assert (st[86][1], st[86][2]) == (83.64, True)
    assert (st[290][1], st[290][2]) == (134.97, False)


def test_candles_and_atm_buffer():
    cd = STS.Candles()
    for t, px in ((0, 10), (20, 12), (59.5, 9), (60, 11), (130, 13)):
        cd.add(t, px)
    assert cd.bars == [{"t": 0, "o": 10, "h": 12, "l": 9, "c": 9}, {"t": 60, "o": 11, "h": 11, "l": 11, "c": 11}]
    assert cd.cur["t"] == 120
    assert STS.buffered_atm(9520, 9400, 200, 20) == 9400              # 120 < 0.7 * 200: stays
    assert STS.buffered_atm(9545, 9400, 200, 20) == 9600


# ── signal state ────────────────────────────────────────────────────────────
OPEN = dt.datetime(2026, 11, 13, 9, 15, tzinfo=IST).timestamp()


def bars_from(closes, t0, spread=1.0):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append({"t": int(t0 + i * 60), "o": prev, "h": max(prev, c) + spread, "l": min(prev, c) - spread, "c": c})
        prev = c
    return out


def sig_with(warm_closes, today_closes):
    s = STS.StSignal("NIFTY")
    s.reset("2026-11-13", "2026-11-17")
    s.open_ts = OPEN
    s.install_warmup(bars_from(warm_closes, OPEN - 86400), [], "replay 2026-11-12", "2026-11-12")
    s.today.bars = bars_from(today_closes, OPEN)
    s._cache = None
    return s


def test_red_at_open_needs_first_candle_low_break():
    s = sig_with([400 - i * 3 for i in range(40)], [281, 279, 278])        # falling premium all along
    st = s.state(10, 3)
    assert st["ready"] and st["trend"] == "red" and st["start_red"] is True and st["flipped_today"] is False
    assert st["first_low"] == 280.0                                       # first candle: o 283, c 281, -1


def test_green_candle_today_switches_to_flip_mode():
    s = sig_with([400 - i * 3 for i in range(40)], [281, 300, 340, 380, 420, 400, 370, 330, 290, 250, 210])
    st = s.state(10, 3)
    assert st["start_red"] is True and st["flipped_today"] is True


def test_not_ready_until_warmup_done():
    s = STS.StSignal("NIFTY")
    s.reset("2026-11-13", "2026-11-17")
    assert s.state()["ready"] is False                                    # warm-up pending: never trade blind


# ── variant book ────────────────────────────────────────────────────────────
class FakeSignal:
    def __init__(self):
        self.s = {"ready": True, "trend": "green", "st": 300.0, "px": 290.0, "start_red": False,
                  "flipped_today": False, "first_low": None}

    def __call__(self, per, mul):
        return dict(self.s)


def variant(universe):
    g = Rig(universe, 9400)
    r = g.eng.runners[N + C.VARIANT_SUFFIX]
    r.ctx.in_session = lambda now=None: True
    fs = FakeSignal()
    r.ctx.signal = fs
    g.eng.start(N + C.VARIANT_SUFFIX)
    return g, r, fs


def lots(r):
    return sum(-p.lots for p in r.book.positions.values() if p.lots < 0)


def test_variant_enters_on_red_and_exits_on_green(universe):
    g, r, fs = variant(universe)
    assert r.strategy_name == "cepe_st" and r.state["phase"] == "flat" and lots(r) == 0
    g.move(9400)                                                           # green: stays flat
    assert lots(r) == 0
    fs.s.update(trend="red", flipped_today=True)
    g.move(9400)
    assert lots(r) == 6 and r.state["phase"] == "in"
    assert any(e["event"] == "st_entry" for e in r.events)
    assert g.eng.runners[N].book.trades == []                              # the normal book is separate
    fs.s.update(trend="green")
    g.move(9405)
    assert lots(r) == 0 and r.state["phase"] == "flat"
    assert any(str(e.get("action", "")).startswith("exit: Supertrend green") for e in r.events)
    fs.s.update(trend="red")
    g.move(9400)
    assert lots(r) == 6                                                    # re-enters at the next red


def test_variant_opening_rule(universe):
    g, r, fs = variant(universe)
    fs.s.update(trend="red", start_red=True, flipped_today=False, first_low=280.0, px=285.0)
    g.move(9400)
    assert lots(r) == 0 and "first candle's low" in (r.state.get("blocked") or "")
    fs.s.update(px=279.5)
    g.move(9400)
    assert lots(r) == 6
    assert any("opening low break" in str(e.get("why", "")) for e in r.events)


def test_variant_adjusts_like_normal_while_red(universe):
    g, r, fs = variant(universe)
    fs.s.update(trend="red", flipped_today=True)
    g.move(9400)
    g.move(9545)                                                           # breach -> normal rules act
    acts = [e["action"] for e in r.events if e["event"] == "adjust"]
    assert acts[0] == "entry" and len(acts) > 1


def test_variant_waits_when_signal_not_ready(universe):
    g, r, fs = variant(universe)
    fs.s.update(ready=False, trend="red")
    g.move(9400)
    assert lots(r) == 0


# ── warm-up replay from recorded quotes ─────────────────────────────────────
def test_replay_bars_from_recorded_quotes(universe, tmp_path):
    """Write 20 minutes of model quotes through the real QuoteRecorder, replay them into candles and
    compare each candle close with the model's ATM CE+PE at that moment."""
    m = Market(universe)
    e = m.expiry(N)
    und = universe.underlyings[N]
    fut = und.future_for(e)
    rec = Q.QuoteRecorder(universe, folder=tmp_path, start=False)
    t0 = dt.datetime(2026, 11, 2, 20, 0, tzinfo=IST).timestamp()
    path = []
    rnd = random.Random(3)
    f = 9400.0
    for i in range(20 * 60 // 5):                                           # a tick set every 5 s
        t = t0 + i * 5
        f += rnd.gauss(0, 8)
        path.append((t, f))
        ticks = [Tick(fut.key, round(f, 1), f - 1, f + 1, 1, 1, 0, 0, int(t), t)]
        for k in range(8600, 10201, 200):
            for kind in ("CE", "PE"):
                c = m.contract(N, e, k, kind)
                if c:
                    px = model(k, kind, f)
                    ticks.append(Tick(c.key, px, px - 0.5, px + 0.5, 1, 1, 0, 0, int(t), t))
        m.apply(ticks)
        for tk in ticks:
            rec.add(N, tk.key, m.quote(tk.key), t)
    rec.flush()
    bars = STS.replay_bars(universe, N, "2026-11-02", e, 200, 20, t0, t0 + 20 * 60, root=tmp_path)
    assert 18 <= len(bars) <= 20
    # recompute the expected close of each candle from the path (ATM with the same buffer rule)
    atm, exp = None, {}
    for t, fpx in path:
        atm = STS.buffered_atm(round(fpx, 1), atm, 200, 20)
        exp[int(t // 60 * 60)] = model(atm, "CE", round(fpx, 1)) + model(atm, "PE", round(fpx, 1))
    for b in bars:
        assert b["c"] == pytest.approx(exp[b["t"]], abs=1.01)              # marks within the +-0.5 quote
    # another expiry was not recorded -> no candles (warm-up falls back to today only)
    other = und.expiries()[1]
    assert STS.replay_bars(universe, N, "2026-11-02", other, 200, 20, t0, t0 + 1200, root=tmp_path) == []


def test_warm_up_picks_previous_recorded_day(universe, tmp_path):
    (tmp_path / "2026-10-30").mkdir()
    (tmp_path / "2026-10-30" / f"{N}.qrec").write_bytes(b"")
    (tmp_path / "2026-11-02").mkdir()
    (tmp_path / "2026-11-02" / f"{N}.qrec").write_bytes(b"")
    assert STS.previous_quote_day(N, "2026-11-03", root=tmp_path) == "2026-11-02"
    assert STS.previous_quote_day(N, "2026-11-02", root=tmp_path) == "2026-10-30"
    assert STS.previous_quote_day("GOLD", "2026-11-03", root=tmp_path) is None
