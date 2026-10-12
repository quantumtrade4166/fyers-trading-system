"""Deterministic tests for the state machine, expiry rule, candles and costs.

    .venv\\Scripts\\python.exe -m pytest backtesting/options_st_directional/test_strategy.py -q
"""
import random

import numpy as np
import pandas as pd
import pytest

from backtesting.options_st_directional.config import Config, lot_size
from backtesting.options_st_directional.engine import run_day
from backtesting.options_st_directional import data as D
from backtesting.options_st_directional import report

DAY = "2026-09-02"
EXP = pd.Timestamp("2026-09-08")
GRID = pd.date_range(f"{DAY} 09:15", f"{DAY} 15:25", freq="5min")


def T(hhmm):
    return pd.Timestamp(f"{DAY} {hhmm}")


def PE(k=25000):
    return (EXP, k, "PE")


def CE(k=25000):
    return (EXP, k, "CE")


class Fake:
    """Spot direction per candle; option candles default to a quiet bar
    (low 100, close 110, ST 200 above price) unless overridden."""

    def __init__(self, dirs=None, spot=None, opts=None, fills=None,
                 missing=(), no_data=(), no_fill=()):
        self.dirs = dirs or {}
        self.spot = spot or {}
        self.opts = opts or {}
        self.fills = fills or {}
        self.missing = set(missing)
        self.no_data = set(no_data)
        self.no_fill = set(no_fill)
        self.selections = []
        self.base_dir = 1

    def spot_candles(self, day):
        d, cur = [], self.base_dir
        for t in GRID:
            cur = self.dirs.get(t.strftime("%H:%M"), cur)
            d.append(cur)
        close, c = [], 25000.0
        for t in GRID:
            c = self.spot.get(t.strftime("%H:%M"), c)
            close.append(c)
        return pd.DataFrame({"close": close, "st": 0.0, "st_dir": d}, index=GRID)

    def select_contract(self, day, ts, spot):
        self.selections.append((ts, spot))
        return {"strike": int(round(spot / 50) * 50), "expiry": EXP, "reason": "test"}

    def option_candles(self, day, contract):
        if contract in self.no_data:
            return pd.DataFrame()
        rows = []
        over = self.opts.get(contract, {})
        idx = []
        for t in GRID:
            key = t.strftime("%H:%M")
            if (contract, key) in self.missing:
                continue
            r = {"low": 100.0, "close": 110.0, "st": 200.0, "st_dir": -1}
            r.update(over.get(key, {}))
            rows.append(r)
            idx.append(t)
        return pd.DataFrame(rows, index=idx)

    def fill(self, day, contract, ts):
        if (contract, ts.strftime("%H:%M")) in self.no_fill:
            return None
        return ts, self.fills.get((contract, ts.strftime("%H:%M")), 50.0)

    def last_price(self, day, contract, ts):
        return ts, 50.0


CFG = Config()


def run(fake):
    return run_day(DAY, fake, CFG)


def ev(r, name):
    return [e for e in r["events"] if e["event"] == name]


# 1, 2 -------------------------------------------------------------------------
def test_green_prepares_pe():
    r = run(Fake())
    s = r["setups"][0]
    assert s.opt_type == "PE" and s.origin == "initial" and s.created_at == T("09:20")


def test_red_prepares_ce():
    f = Fake(); f.base_dir = -1
    assert run(f)["setups"][0].opt_type == "CE"


# 3, 4 -------------------------------------------------------------------------
def test_first_entry_evaluation_at_0925():
    # the 09:15 reference candle itself closes far below its own low: must not count
    f = Fake(opts={PE(): {"09:15": {"low": 100, "close": 90},
                          "09:20": {"low": 95, "close": 95}}})
    r = run(f)
    sig = ev(r, "entry_signal")
    assert sig[0]["ts"] == T("09:25")
    assert r["trades"][0]["entry_ts"] == T("09:25")
    assert r["trades"][0]["signal_ref"] == 100


def test_close_strictly_below_signals():
    f = Fake(opts={PE(): {"09:20": {"low": 99.0, "close": 99.95}}})
    assert len(run(f)["trades"]) == 1


# 5 ----------------------------------------------------------------------------
def test_close_equal_to_reference_does_not_signal():
    f = Fake(opts={PE(): {"09:20": {"low": 100.0, "close": 100.0}}})
    r = run(f)
    assert not r["trades"] and not ev(r, "entry_signal") and not ev(r, "reference_lowered")


# 6 ----------------------------------------------------------------------------
def test_prompt_example_literal_96_below_97_is_a_signal():
    # The prompt's illustration says "low 94, close 96: no entry" after the
    # reference became 97 -- but 96 < 97 satisfies the stated rule, so it fires.
    f = Fake(opts={PE(): {"09:20": {"low": 97, "close": 101},
                          "09:25": {"low": 94, "close": 96}}})
    tr = run(f)["trades"][0]
    assert tr["signal_ts"] == T("09:30") and tr["signal_ref"] == 97


def test_wicks_move_reference_down():
    f = Fake(opts={PE(): {"09:20": {"low": 97, "close": 101},
                          "09:25": {"low": 94, "close": 98},
                          "09:30": {"low": 91, "close": 95},
                          "09:35": {"low": 89, "close": 90}}})
    r = run(f)
    lows = [e["detail"] for e in ev(r, "reference_lowered")]
    assert len(lows) == 3 and "new=91" in lows[-1]
    tr = r["trades"][0]
    assert tr["signal_ts"] == T("09:40") and tr["signal_ref"] == 91 and tr["signal_close"] == 90


def test_reference_never_moves_up():
    f = Fake(opts={PE(): {"09:20": {"low": 97, "close": 101},
                          "09:25": {"low": 99, "close": 101},     # higher low: ignored
                          "09:30": {"low": 98, "close": 97.5}}})  # 97.5 < 97? no
    r = run(f)
    assert not r["trades"]
    assert r["setups"][0].ref_low == 97


# 7, 8 -------------------------------------------------------------------------
def test_option_close_above_st_exits():
    f = Fake(opts={PE(): {"09:20": {"close": 95, "low": 95},
                          "09:25": {"close": 120, "st": 110}}})
    tr = run(f)["trades"][0]
    assert tr["exit_reason"] == "option_st" and tr["exit_ts"] == T("09:30")
    assert tr["exit_close"] == 120 and tr["exit_st"] == 110


def test_option_close_equal_st_does_not_exit():
    f = Fake(opts={PE(): {"09:20": {"close": 95, "low": 95},
                          "09:25": {"close": 110, "st": 110}}})
    tr = run(f)["trades"][0]
    assert tr["exit_reason"] == "square_off_1515" and tr["exit_ts"] == T("15:15")


# 9, 10, 11 --------------------------------------------------------------------
def reversal_fake(**kw):
    """PE sold 09:25; spot flips bearish on the 10:00 candle at 25100."""
    opts = {PE(): {"09:20": {"close": 95, "low": 95}}, CE(25100): {}}
    for k, v in kw.pop("opts", {}).items():
        opts.setdefault(k, {}).update(v)
    return Fake(dirs={"10:00": -1, **kw.pop("dirs", {})},
                spot={"10:00": 25100.0, **kw.pop("spot", {})}, opts=opts, **kw)


def test_reversal_does_not_close_position():
    r = run(reversal_fake())
    assert len(r["trades"]) == 1
    assert r["trades"][0]["exit_reason"] == "square_off_1515"
    assert ev(r, "spot_reversal")[0]["ts"] == T("10:05")


def test_reversal_setup_uses_reversal_time_atm():
    f = reversal_fake()
    r = run(f)
    s = r["setups"][1]
    assert s.origin == "reversal" and s.opt_type == "CE" and s.strike == 25100
    assert s.selection_ts == T("10:05") and s.spot_at_selection == 25100.0


def test_reversal_reference_is_reversal_candle():
    f = reversal_fake(opts={CE(25100): {"10:00": {"low": 88, "close": 90}}})
    s = run(f)["setups"][1]
    assert s.ref_candle_ts == T("10:00") and s.ref_low_initial == 88


# 12, 13, 14, 17 ---------------------------------------------------------------
def test_reference_updates_and_pending_while_position_open():
    f = reversal_fake(opts={
        CE(25100): {"10:00": {"low": 100, "close": 105},
                    "10:05": {"low": 96, "close": 101},     # wick -> ref 96
                    "10:10": {"low": 94, "close": 95},      # 95 < 96 -> signal @10:15
                    "10:20": {"low": 60, "close": 61}},     # later lower candle: irrelevant
        PE(): {"10:20": {"close": 150, "st": 120}}})        # PE exit signal @10:25
    r = run(f)
    assert [e["ts"] for e in ev(r, "reference_lowered") if e["setup_id"].endswith("S2")] == [T("10:10")]
    assert ev(r, "signal_pending")[0]["ts"] == T("10:15")
    pe, ce = r["trades"]
    assert pe["exit_ts"] == T("10:25")
    assert ce["opt_type"] == "CE" and ce["pending_while_open"]
    assert ce["signal_ts"] == T("10:15") and ce["signal_ref"] == 96
    assert ce["entry_ts"] == T("10:25") and ce["prev_position_closed_ts"] == T("10:25")


def test_reference_not_reset_when_previous_closes():
    f = reversal_fake(opts={
        CE(25100): {"10:00": {"low": 90, "close": 95},
                    "10:20": {"low": 92, "close": 93},      # higher than 90: no change
                    "10:25": {"low": 89.5, "close": 89.9}},  # 89.9 < 90 -> signal
        PE(): {"10:20": {"close": 150, "st": 120}}})
    r = run(f)
    ce = r["trades"][1]
    assert ce["ref_low_initial"] == 90 and ce["signal_ref"] == 90
    assert not ce["pending_while_open"] and ce["signal_ts"] == T("10:30")


def test_no_signal_by_exit_keeps_waiting():
    f = reversal_fake(opts={PE(): {"10:20": {"close": 150, "st": 120}}})
    r = run(f)
    assert len(r["trades"]) == 1 and r["setups"][1].status == "expired_eod"


# 15, 16 -----------------------------------------------------------------------
def test_pending_invalidated_by_second_reversal():
    f = reversal_fake(
        dirs={"10:15": 1}, spot={"10:15": 25200.0},
        opts={CE(25100): {"10:05": {"low": 90, "close": 91}},     # signal @10:10
              PE(): {"10:30": {"close": 150, "st": 120}}})        # old PE exits @10:35
    r = run(f)
    inval = ev(r, "setup_invalidated")
    assert inval and "had_pending_signal=True" in inval[0]["detail"]
    assert all(t["opt_type"] != "CE" for t in r["trades"])


def test_latest_direction_wins_after_multiple_reversals():
    f = reversal_fake(
        dirs={"10:10": 1, "10:20": -1, "10:30": 1},
        spot={"10:10": 25150.0, "10:20": 25050.0, "10:30": 25200.0},
        opts={PE(25200): {"10:35": {"low": 80, "close": 81}},     # signal @10:40
              PE(): {"10:45": {"close": 150, "st": 120}}})        # old PE exits @10:50
    r = run(f)
    assert len(ev(r, "spot_reversal")) == 4
    last = r["trades"][-1]
    assert last["strike"] == 25200 and last["opt_type"] == "PE" and last["entry_ts"] == T("10:50")
    assert last["setup_id"] == r["setups"][-1].setup_id


# 18 ---------------------------------------------------------------------------
def test_expiry_switch_two_sessions_left():
    from backtesting.options_credit_spread.expiry_calendar import ExpiryCalendar
    p = D.BreezeProvider.__new__(D.BreezeProvider)
    p.cfg = Config()
    p.cal = ExpiryCalendar.__new__(ExpiryCalendar)
    p.cal.expiry_dates = [pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-08"),
                          pd.Timestamp("2026-09-15")]
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-09-01", "2026-09-16")]
    p.cal.set_trading_days(days)
    pick = lambda d: p.select_contract(d, None, 25012)["expiry"].strftime("%m-%d")
    assert pick("2026-09-02") == "09-08"     # Wed: 4 left
    assert pick("2026-09-03") == "09-08"     # Thu: 3 left
    assert pick("2026-09-04") == "09-15"     # Fri: 2 left -> next week
    assert pick("2026-09-07") == "09-15"     # Mon
    assert pick("2026-09-08") == "09-15"     # expiry day
    assert p.select_contract("2026-09-02", None, 25012)["strike"] == 25000
    # holiday week: drop Fri 4-Sep -> Thu now has only Mon+Tue left -> next week
    p.cal.set_trading_days([d for d in days if d != "2026-09-04"])
    assert pick("2026-09-03") == "09-15"


# 19 ---------------------------------------------------------------------------
def test_missing_option_candle_creates_no_signal():
    f = Fake(opts={PE(): {"09:25": {"low": 99, "close": 99.5}}},
             missing=[(PE(), "09:20")])
    r = run(f)
    assert ev(r, "option_candle_missing")[0]["ts"] == T("09:25")
    assert r["trades"][0]["signal_ts"] == T("09:30")


def test_to_5min_never_fills_gaps():
    t = pd.date_range(f"{DAY} 09:15", periods=15, freq="1min")
    df = pd.DataFrame({"datetime": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5})
    df = df[~df.datetime.dt.strftime("%H:%M").isin(["09:20", "09:21", "09:22", "09:23", "09:24"])]
    c5 = D.to_5min(df)
    assert list(c5.index.strftime("%H:%M")) == ["09:15", "09:25"]


def test_no_data_contract_never_trades():
    f = Fake(no_data=[PE()])
    r = run(f)
    assert r["setups"][0].status == "no_data" and not r["trades"]


def test_missing_entry_fill_retries_not_fabricated():
    f = Fake(opts={PE(): {"09:20": {"low": 95, "close": 95}}}, no_fill=[(PE(), "09:25")])
    tr = run(f)["trades"][0]
    assert tr["signal_ts"] == T("09:25") and tr["entry_ts"] == T("09:30")


# 20 ---------------------------------------------------------------------------
def test_future_candles_do_not_change_past_decisions():
    rng = random.Random(7)

    def random_fake(seed_future):
        r2 = random.Random(seed_future)
        opts, dirs = {}, {}
        for k in (24950, 25000, 25050):
            for typ in ("PE", "CE"):
                o = {}
                for t in GRID:
                    hh = t.strftime("%H:%M")
                    src = rng if hh < "11:00" else r2
                    lo = src.uniform(60, 110)
                    o[hh] = {"low": lo, "close": lo + src.uniform(0, 15),
                             "st": src.uniform(80, 130)}
                opts[(EXP, k, typ)] = o
        return opts

    base_state = rng.getstate()
    a_opts = random_fake(1)
    rng.setstate(base_state)
    b_opts = random_fake(2)
    dirs = {"09:45": -1, "10:20": 1, "11:30": -1, "13:00": 1}
    spot = {"09:45": 24960.0, "10:20": 25040.0, "11:30": 24990.0, "13:00": 25010.0}
    ra = run(Fake(dirs=dirs, spot=spot, opts=a_opts))
    rb = run(Fake(dirs=dirs, spot=spot, opts=b_opts))
    cut = T("11:00")         # candles from 11:00 differ; decisions up to 11:00 must match
    pa = [(e["ts"], e["event"], e["detail"]) for e in ra["events"] if e["ts"] <= cut]
    pb = [(e["ts"], e["event"], e["detail"]) for e in rb["events"] if e["ts"] <= cut]
    assert pa == pb


def test_fills_never_precede_signals():
    f = Fake(opts={PE(): {"09:20": {"low": 95, "close": 95}, "09:40": {"close": 150, "st": 120}}})
    tr = run(f)["trades"][0]
    assert tr["entry_ts"] >= tr["signal_ts"] and tr["exit_ts"] >= tr["exit_signal_ts"]
    assert tr["exit_ts"] == T("09:45")


# 21 ---------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(25))
def test_never_two_positions(seed):
    rng = random.Random(seed)
    dirs, spot, cur = {}, {}, 1
    for t in GRID[1:]:
        if rng.random() < 0.08:
            cur = -cur
            dirs[t.strftime("%H:%M")] = cur
            spot[t.strftime("%H:%M")] = 25000 + 50 * rng.randint(-3, 3)
    opts = {}
    for k in range(24850, 25200, 50):
        for typ in ("PE", "CE"):
            opts[(EXP, k, typ)] = {}
            for t in GRID:
                lo = rng.uniform(60, 110)
                opts[(EXP, k, typ)][t.strftime("%H:%M")] = {
                    "low": lo, "close": lo + rng.uniform(0, 12), "st": rng.uniform(70, 130)}
    r = run(Fake(dirs=dirs, spot=spot, opts=opts))
    tr = sorted(r["trades"], key=lambda x: x["entry_ts"])
    for a, b in zip(tr, tr[1:]):
        assert b["entry_ts"] >= a["exit_ts"]
    for x in tr:
        assert x["exit_ts"] <= T("15:15") and x["entry_ts"] < T("15:15")


# square-off -------------------------------------------------------------------
def test_signal_at_1515_is_not_entered():
    f = Fake(opts={PE(): {"15:10": {"low": 90, "close": 91}}})
    r = run(f)
    assert not r["trades"]


# 22 ---------------------------------------------------------------------------
def test_costs_and_lot_size():
    assert lot_size("2026-09-02") == 65 and lot_size("2025-06-02") == 75
    with pytest.raises(ValueError):
        lot_size("2023-01-02")
    tr = pd.DataFrame([{"date": DAY, "entry_price": 100.0, "exit_price": 80.0,
                        "entry_ts": T("09:25"), "exit_ts": T("10:00"),
                        "exit_signal_ts": T("10:00"), "exit_reason": "option_st",
                        "pending_while_open": False}])
    out = report.add_pnl(tr, Config(slippage_points=0.5))
    r = out.iloc[0]
    assert r.qty == 65 and r.gross_pnl == pytest.approx(1300.0)
    assert r.cost_stt == pytest.approx(100 * 65 * 0.001)
    assert r.cost_brokerage == 40.0 and r.cost_slippage == pytest.approx(65.0)
    assert r.net_pnl == pytest.approx(r.gross_pnl - r.cost_total)
    assert r.cost_total > r.cost_brokerage + r.cost_stt
