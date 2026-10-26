"""Tests for the Kotak D live leg with a fake broker (no network, no real orders).

    .venv\\Scripts\\python.exe -m pytest live_trading_options/nifty_st_directional/test_live_kotak.py -q
"""
import datetime as dt
import json

import pandas as pd
import pytest

import live_trading_options.nifty_st_directional.live_kotak as LK


class FakeKE:
    """Stand-in for kotak_executor: records orders, fills them per a script."""

    def __init__(self, fill_plan=None, margin=False, reject=0, never_settle=False):
        self.orders = []
        self.never_settle = never_settle            # cancel is ignored, status stays "open"
        self.resolves = 0
        self.fill_plan = list(fill_plan or [])      # filled qty per order (default: full)
        self.margin = margin
        self.reject = reject
        self.status = {}
        self.cancels = []
        self.book = []                              # COMPLETE orders for strategy_fills

    def resolve(self, client, index, expiry, strike, opt):
        self.resolves += 1
        return {"trading_symbol": f"NIFTY26O27{strike}{opt}", "exchange_segment": "nse_fo",
                "lot_size": 65, "token": "123"}

    def marketable_limit(self, mark, side):
        return float(mark or 100)

    def marketable_price(self, client, token, seg, side, qty, fallback=None, cushion_ticks=2):
        return fallback

    def is_margin_error(self, ex):
        return "margin" in str(ex).lower()

    def place_limit(self, client, sym, seg, side, qty, price, product="NRML", tag="x"):
        if self.margin and side.upper().startswith("S"):
            raise RuntimeError("RMS: insufficient margin")
        if self.reject > 0:
            self.reject -= 1
            raise RuntimeError("rejected: price band")
        oid = f"O{len(self.orders) + 1}"
        got = self.fill_plan.pop(0) if self.fill_plan else qty
        self.orders.append({"oid": oid, "side": side, "sym": sym, "qty": qty, "price": price, "tag": tag})
        self.status[oid] = {"status": "complete" if got == qty else "open", "filled_qty": got,
                            "avg_price": price}
        if got:
            self.book.append({"trading_symbol": sym, "side": side[0], "qty": got, "avg_price": price,
                              "order_id": oid, "fill_time": "x"})
        return oid

    def order_status(self, client, oid):
        return self.status[oid]

    def cancel(self, client, oid):
        self.cancels.append(oid)
        if not self.never_settle:
            self.status[oid]["status"] = "cancelled"

    def quote_book(self, client, token, seg):
        return {"low_lpp": 10.0, "high_lpp": 900.0}

    def strategy_fills(self, client, tag_prefix):
        return list(self.book)


class FakeMachine:
    def __init__(self):
        self.position = None
        self.trades = []


class FakeEngine:
    def __init__(self, now):
        self.now = now
        self.machine = FakeMachine()
        self.lot_size = 65
        self.events = []
        self.prices = {}

    def clock(self):
        return self.now

    def event(self, kind, msg):
        self.events.append((kind, msg))

    def contract_key(self, c):
        return (pd.Timestamp(c[0]).date(), int(c[1]), c[2])

    def price_cached(self, key):
        return self.prices.get(key, 100.0)

    def ltp_fresh(self, key, max_age_s):
        return self.fresh.get(key) if hasattr(self, "fresh") else None


@pytest.fixture(autouse=True)
def tmp_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(LK, "STATE_DIR", tmp_path)
    monkeypatch.setattr(LK, "RESULTS", tmp_path)
    monkeypatch.setattr(LK, "CONTROL", tmp_path / "ctl.json")
    yield


def make(now=dt.datetime(2026, 10, 19, 9, 30, 2), **ke_kw):
    eng = FakeEngine(now)
    ke = FakeKE(**ke_kw)
    live = LK.KotakDLive(eng, ke=ke, login=lambda: object(), sleep=lambda s: None)
    live.wait_first = live.wait_retry = 0.05            # test clock: no real 3-4 s waits
    live.confirm_s = 0.05
    return eng, ke, live


def open_paper(eng, tid="T1", ts="2026-10-19 09:30:01", price=200.0):
    eng.machine.position = {"trade_id": tid, "entry_ts": pd.Timestamp(ts), "entry_price": price,
                            "contract": (pd.Timestamp("2026-10-27"), 22950, "CE")}
    eng.machine.trades.append(dict(eng.machine.position, exit_reason=None))


def close_paper(eng, reason="option_st"):
    eng.machine.trades[-1]["exit_reason"] = reason
    eng.machine.position = None


def test_disarmed_sends_nothing():
    eng, ke, live = make()
    open_paper(eng)
    live.sync()
    assert ke.orders == [] and live.pos is None


def test_armed_mirrors_entry_and_exit_with_lots():
    eng, ke, live = make()
    LK.write_control(armed=True, lots=3)
    open_paper(eng)
    live.sync()
    assert ke.orders[0]["side"] == "SELL" and ke.orders[0]["qty"] == 195 and ke.orders[0]["tag"] == "stdk"
    assert live.pos["qty"] == 195
    live.sync()                                    # same trade: nothing new
    assert len(ke.orders) == 1
    close_paper(eng, "breakeven_stop")
    live.sync()
    assert ke.orders[1]["side"] == "BUY" and ke.orders[1]["qty"] == 195 and live.pos is None


def test_never_chases_an_entry_older_than_arming():
    eng, ke, live = make(now=dt.datetime(2026, 10, 19, 10, 5, 0))
    LK.write_control(armed=True, lots=1)
    open_paper(eng, ts="2026-10-19 09:30:01")     # paper sold 35 min ago
    live.sync()
    assert ke.orders == [] and "T1" in live.skipped


def test_unfilled_order_is_cancelled_and_retried():
    eng, ke, live = make(fill_plan=[0, 65])
    LK.write_control(armed=True, lots=1)
    open_paper(eng)
    live.sync()
    assert ke.cancels == ["O1"] and len(ke.orders) == 2 and live.pos["qty"] == 65


def test_margin_rejection_halts_for_the_day():
    eng, ke, live = make(margin=True)
    LK.write_control(armed=True, lots=1)
    open_paper(eng)
    live.sync()
    assert live.pos is None and live.halted == "margin rejection"
    close_paper(eng)
    open_paper(eng, tid="T2", ts="2026-10-19 09:30:01")
    live.sync()
    assert "T2" in live.skipped and ke.orders == []


def test_kill_flattens_and_disarms():
    eng, ke, live = make()
    LK.write_control(armed=True, lots=2)
    open_paper(eng)
    live.sync()
    LK.write_control(kill=True)
    live.sync()
    assert ke.orders[-1]["side"] == "BUY" and live.pos is None
    c = LK.read_control()
    assert c["armed"] is False and c["kill"] is False and live.halted.startswith("killed")


def test_incomplete_buyback_keeps_retrying_until_flat():
    eng, ke, live = make(fill_plan=[65, 0, 0, 0, 0, 0, 0, 0, 0])
    LK.write_control(armed=True, lots=1)
    open_paper(eng)
    live.sync()
    close_paper(eng)
    live.sync()                                    # 4 attempts, nothing fills
    assert live.pos["qty"] == 65 and live.pending_exit
    ke.fill_plan = []                              # broker now fills
    live.sync()
    assert live.pos is None and live.pending_exit is None


def test_large_orders_are_sliced_at_freeze_qty():
    assert LK.KotakDLive._slices(1950) == [1800, 150]
    assert LK.KotakDLive._slices(650) == [650]


def test_restart_rebuilds_open_short_from_own_orders():
    eng, ke, live = make()
    LK.write_control(armed=True, lots=2)
    open_paper(eng)
    live.sync()
    eng2 = FakeEngine(eng.now)
    eng2.machine = eng.machine
    live2 = LK.KotakDLive(eng2, ke=ke, login=lambda: object(), sleep=lambda s: None)
    live2.wait_first = live2.wait_retry = live2.confirm_s = 0.05
    live2.reconcile()
    assert live2.pos["qty"] == 130 and live2.pos["trade_id"] == "T1"
    close_paper(eng2)
    live2.sync()
    assert ke.orders[-1]["side"] == "BUY" and ke.orders[-1]["qty"] == 130 and live2.pos is None


def test_safety_squareoff_after_1516():
    eng, ke, live = make()
    LK.write_control(armed=True, lots=1)
    open_paper(eng)
    live.sync()
    eng.now = dt.datetime(2026, 10, 19, 15, 16, 5)
    live.sync()
    assert ke.orders[-1]["side"] == "BUY" and live.pos is None


def test_realized_pairs_fifo_per_contract():
    """26-Oct: sold 22450CE 267.5 / bought 356.2, then sold 22550PE 164.85 (open).
    True realized = -88.7 x 65 = -5,765.5 (the old average-of-everything showed ~ -9,100)."""
    fills = [{"side": "SELL", "symbol": "CE", "qty": 65, "price": 267.5},
             {"side": "BUY", "symbol": "CE", "qty": 65, "price": 356.2},
             {"side": "SELL", "symbol": "PE", "qty": 65, "price": 164.85}]
    assert LK.realized_fifo(fills) == pytest.approx(-5765.5)


def test_prepared_contract_is_not_looked_up_at_fire_time():
    eng, ke, live = make()
    LK.write_control(armed=True, lots=1)
    live.prepare((pd.Timestamp("2026-10-27"), 22950, "CE"))
    assert ke.resolves == 1
    open_paper(eng)
    live.sync()
    assert ke.resolves == 1 and ke.orders[0]["side"] == "SELL"


def test_first_order_priced_from_own_tick_inside_band():
    eng, ke, live = make()
    eng.fresh = {(dt.date(2026, 10, 27), 22950, "CE"): 270.0}
    LK.write_control(armed=True, lots=1)
    live.prepare((pd.Timestamp("2026-10-27"), 22950, "CE"))
    open_paper(eng)
    eng.last_decision_ms = LK._ms()
    live.sync()
    assert ke.orders[0]["price"] == pytest.approx(267.3)          # 1% through the 270.0 tick
    f = live.fills[0]
    assert f["price_src"].startswith("tick") and f["ms_decision_to_send"] >= 0 and "ms_ack" in f


def test_unknown_order_state_blocks_every_new_order():
    eng, ke, live = make(fill_plan=[0], never_settle=True)
    LK.write_control(armed=True, lots=1)
    open_paper(eng)
    live.sync()
    assert len(ke.orders) == 1 and live.unsettled and live.pos is None
    close_paper(eng)
    open_paper(eng, tid="T2")
    live.sync()                                       # still unknown -> nothing new is sent
    assert len(ke.orders) == 1
    # Kotak finally reports the order: filled
    oid = ke.orders[0]["oid"]
    ke.status[oid].update(status="complete", filled_qty=65)
    live.sync()
    assert not live.unsettled and any(f["attempt"] == "settled" for f in live.fills)


def test_view_shows_trades_position_and_stop_to_cost():
    eng, ke, live = make()
    eng.p = {"breakeven_after_points": 25}
    LK.write_control(armed=True, lots=1)
    open_paper(eng, price=200.0)
    live.sync()
    v = live.view()
    pd_ = v["pos_detail"]
    assert pd_["qty"] == 65 and pd_["expiry"] == "2026-10-27" and pd_["strike"] == 22950
    assert pd_["stop"] == {"price": 200.0, "trigger": 175.0, "points": 25, "armed": None}
    assert len(v["trades"]) == 1 and v["trades"][0]["open"] and v["trades"][0]["trade_id"] == "T1"
    close_paper(eng, "breakeven_stop")
    live.sync()
    t = live.view()["trades"][0]
    assert not t["open"] and t["reason"] == "breakeven_stop" and t["net"] is not None
