"""
vwap/test_vwap.py — offline tests for the BTC VWAP strangle. No network.

    .venv/Scripts/python.exe live_trading_options/delta_btc/vwap/test_vwap.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import tempfile
import datetime as dt

import vwap.book as book_mod
from vwap.strategy import Session, select_pair, CandleBuilder, Trigger, bucket_start
from vwap.book import VwapBook

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


D = dt.datetime
IST = {"label": "ist", "start": "09:30", "entry_cutoff": "16:25", "square_off": "17:10",
       "combined_threshold": 100, "contracts": 1000, "max_entries": 4, "mtm_stop_usd": 50}
FULL = dict(IST, start="17:35", max_entries=8)

# ── sessions ──────────────────────────────────────────────────────────────
s = Session("ist_day", IST)
check("ist in session", s.bounds(D(2026, 9, 16, 10, 0)) is not None)
check("ist before start", s.bounds(D(2026, 9, 16, 9, 29)) is None)
check("ist after square-off", s.bounds(D(2026, 9, 16, 17, 10)) is None)
b = s.bounds(D(2026, 9, 16, 10, 0))
check("ist expiry today", Session.expiry_date(b) == dt.date(2026, 9, 16))
check("ist cutoff", s.cutoff_dt(b) == D(2026, 9, 16, 16, 25))

f = Session("full_expiry", FULL)
b = f.bounds(D(2026, 9, 15, 21, 42))
check("full evening bounds", b == (D(2026, 9, 15, 17, 35), D(2026, 9, 16, 17, 10)))
check("full expiry tomorrow", Session.expiry_date(b) == dt.date(2026, 9, 16))
check("full cutoff next day", f.cutoff_dt(b) == D(2026, 9, 16, 16, 25))
b2 = f.bounds(D(2026, 9, 16, 3, 0))
check("full after midnight same cycle", b2 == b)
check("full gap", f.bounds(D(2026, 9, 16, 17, 20)) is None)

# ── strike selection ──────────────────────────────────────────────────────
strikes = [74800, 75000, 75500, 75800, 76000, 76200, 76400, 76600, 76800, 77000, 77500, 78000]
marks = {}
for k in strikes:
    marks[(k, "CE")] = max(1, (78200 - k) / 20)       # cheaper further up
    marks[(k, "PE")] = max(1, (k - 74800) / 20)       # cheaper further down
p = select_pair(marks, strikes, 76410, 100)
check("atm nearest", p and p["atm"] == 76400)
check("equidistant", p and (p["ce"] - 76400) == (76400 - p["pe"]))
check("pair under threshold", p and p["combined"] <= 100)
check("first equidistant level under threshold (150/130/110 rejected, 77500 has no mirror)", p and p["ce"] == 78000 and p["pe"] == 74800 and not p["over_threshold"])
check("never sells ATM", p and p["ce"] != 76400 and p["pe"] != 76400)
check("none when nothing cheap", select_pair(marks, strikes, 76410, 5) is None)
p4 = select_pair(marks, strikes, 76410, 120)
check("skips level with no listed mirror", p4 and p4["ce"] == 77000 and p4["pe"] == 75800)
p3 = select_pair(marks, strikes, 76410, 5, fallback_last=True)
check("fallback = farthest equidistant, flagged", p3 and p3["ce"] == 78000 and p3["pe"] == 74800 and p3["over_threshold"])

# ── candles + vwap ────────────────────────────────────────────────────────
cb = CandleBuilder(D(2026, 9, 16, 9, 30))
check("pre-start sample ignored", cb.add(D(2026, 9, 16, 9, 29, 59), 50) is None and cb.cur is None)
for sec, v in ((1, 100), (60, 110), (120, 90), (240, 95)):
    cb.add(D(2026, 9, 16, 9, 30) + dt.timedelta(seconds=sec), v)
closed = cb.add(D(2026, 9, 16, 9, 35, 1), 96)
check("candle closes on new bucket", closed is not None)
check("ohlc", closed and (closed["open"], closed["high"], closed["low"], closed["close"]) == (100, 110, 90, 95))
c1 = cb.finalize(closed, 10)
check("vwap = typical on first", abs(c1["vwap"] - (110 + 90 + 95) / 3) < 1e-3)
raw2 = cb.due(D(2026, 9, 16, 9, 40, 0))
check("clock close", raw2 is not None)
c2 = cb.finalize(raw2, 30)
check("vwap volume-weighted", abs(c2["vwap"] - ((295 / 3) * 10 + 96 * 30) / 40) < 1e-3)
cb0 = CandleBuilder(D(2026, 9, 16, 9, 30))
cb0.add(D(2026, 9, 16, 9, 30, 5), 10)
k0 = cb0.finalize(cb0.due(D(2026, 9, 16, 9, 35)), 0)
check("zero volume -> twap fallback", k0["vwap"] == 10 and k0["vwap_src"] == "twap")
rt = CandleBuilder.from_dict(json.loads(json.dumps(cb.to_dict())))
check("builder roundtrip", len(rt.closed) == 2 and rt.cum_v == 40)

# smoothed wicks: a mid-minute spike is NOT a wick; minute opens/closes are
sw = CandleBuilder(D(2026, 9, 16, 9, 30))
for t, v in (((9, 30, 1), 100), ((9, 30, 30), 140), ((9, 30, 59), 101),   # spike 140 mid-minute
             ((9, 31, 1), 102), ((9, 31, 20), 60), ((9, 31, 58), 98),     # dip 60 mid-minute
             ((9, 34, 2), 97), ((9, 34, 58), 99)):
    sw.add(D(2026, 9, 16, *t), v)
k = sw.due(D(2026, 9, 16, 9, 35))
check("smoothed high = max minute open/close", k and k["high"] == 102)
check("smoothed low = min minute open/close", k and k["low"] == 97)
check("smoothed open/close exact", k and k["open"] == 100 and k["close"] == 99)


# ── trigger ───────────────────────────────────────────────────────────────
def cndl(h, m, o, hi, lo, c, vwap):
    return {"start": D(2026, 9, 16, h, m), "open": o, "high": hi, "low": lo, "close": c,
            "vwap": vwap}


ev = []
tr = Trigger(lambda t, n, r: ev.append(("E", t, n)), lambda n, r: ev.append(("X", n)),
             max_entries=2, cutoff=D(2026, 9, 16, 16, 25))
tr.on_candle_close(cndl(9, 30, 100, 101, 95, 96, 98))            # red below -> arm 94
check("arm at low-1", tr.pending and tr.pending["trigger"] == 94)
tr.on_tick(95, D(2026, 9, 16, 9, 36))
check("no fill above trigger", not ev)
tr.on_tick(94, D(2026, 9, 16, 9, 37))
check("fill at trigger", ev == [("E", 94, 1)] and tr.in_pos)
tr.on_candle_close(cndl(9, 35, 96, 99, 93, 99, 97))              # entry candle: no exit
check("no exit on entry candle", tr.in_pos)
tr.on_candle_close(cndl(9, 40, 99, 100, 97, 98, 97.5))           # close above vwap
check("exit on close above", ev[-1] == ("X", 1) and not tr.in_pos)
tr.on_candle_close(cndl(9, 45, 98, 99, 90, 91, 95))              # arm 89
tr.on_candle_close(cndl(9, 50, 91, 92, 85, 86, 94))              # replace 84
check("replace at new low-1", tr.pending["trigger"] == 84)
tr.on_candle_close(cndl(9, 55, 86, 97, 86, 96, 93))              # above -> cancel
check("cancel on close above", tr.pending is None)
tr.on_candle_close(cndl(10, 0, 96, 96, 90, 92, 93))
check("green-or-red below re-arms", tr.pending["trigger"] == 89)
tr.on_candle_close(cndl(10, 5, 92, 95, 91, 94, 93))              # green, above? 94>93 above -> cancel
check("cancel green", tr.pending is None)
tr.on_candle_close(cndl(10, 10, 94, 94, 88, 90, 93))
tr.on_tick(80, D(2026, 9, 16, 10, 16))
tr.on_candle_close(cndl(10, 20, 90, 99, 88, 95, 92))
check("second cycle", [e for e in ev if e[0] == "E"][-1][2] == 2)
tr.on_candle_close(cndl(10, 25, 95, 95, 80, 81, 92))
check("max entries blocks arming", tr.pending is None)

tr2 = Trigger(lambda t, n, r: ev.append(1), lambda n, r: None, max_entries=4,
              cutoff=D(2026, 9, 16, 16, 25))
tr2.on_candle_close(cndl(16, 25, 100, 101, 95, 96, 98))
check("no arm after cutoff", tr2.pending is None)
tr3 = Trigger(lambda t, n, r: False, lambda n, r: None, max_entries=4,
              cutoff=D(2026, 9, 16, 16, 25))
tr3.on_candle_close(cndl(10, 0, 100, 101, 95, 96, 98))
tr3.on_tick(90, D(2026, 9, 16, 10, 6))
tr3.on_tick(90, D(2026, 9, 16, 10, 7))
check("refused fill never re-fires", tr3.entries == 1 and not tr3.in_pos and tr3.pending is None)
tr4 = Trigger(lambda t, n, r: "retry", lambda n, r: None, max_entries=4,
              cutoff=D(2026, 9, 16, 16, 25))
tr4.on_candle_close(cndl(10, 0, 100, 101, 95, 96, 98))
tr4.on_tick(90, D(2026, 9, 16, 10, 6))
check("a rejected book fill stays armed and costs no entry",
      tr4.entries == 0 and not tr4.in_pos and tr4.pending and tr4.pending["trigger"] == 94)


# ── book end-to-end on a fake chain ───────────────────────────────────────
class FakeChain:
    def __init__(self):
        self.strikes = strikes
        self.mark = dict(marks)
        self.spot = 76410.0
        self.contract_value = 0.001
        self.ask = {}

    def is_ready(self):
        return True

    def symbol_for(self, k, t):
        return f"{'C' if t == 'CE' else 'P'}-BTC-{int(k)}-160926"

    def book_fill(self, k, t, side, qty):
        # a 1% spread each way, like a real book: proportional, not a flat 0.5,
        # so the bad-fill guard sees the same relative cost at any premium
        m = self.mark[(k, t)]
        return {"price": round(m * (0.99 if side == "SELL" else 1.01), 4), "source": "l2"}

    def seconds_to_settlement(self, now):
        return 1000


book_mod.api_candles = lambda sym, res, a, b: [{"time": a, "volume": 10}]
fc = FakeChain()
tmp = Path(tempfile.mkdtemp())
params = {"versions": {"ist_day": IST}, "trigger_offset": 1.0, "candle_minutes": 5,
          "fees": {"taker_rate_notional": 0.0001, "premium_cap_rate": 0.035}}
bk = VwapBook("ist_day", params, tmp, log=lambda m: None)
gc = lambda code: fc
t0 = D(2026, 9, 16, 9, 30, 1)
bk.on_poll(t0, gc)
check("book started", bk.active and bk.pair["ce"] == 78000 and not bk.late_start)
ce_k, pe_k = (bk.pair["ce"], "CE"), (bk.pair["pe"], "PE")
bk.builder.cur = None          # drop the selection-time sample; script the candle below


def arm(bk):
    """Arm the trigger just above what the fake book will fill at, so the
    bad-fill guard (5% of the trigger) passes."""
    ce = fc.mark[(bk.pair["ce"], "CE")]
    pe = fc.mark[(bk.pair["pe"], "PE")]
    bk.trigger.pending = {"trigger": round(ce + pe, 4), "signal": "x"}


def fire(bk, now):
    """Arm at the current book price and send the tick that fills it."""
    arm(bk)
    ce = fc.mark[(bk.pair["ce"], "CE")]
    pe = fc.mark[(bk.pair["pe"], "PE")]
    bk.on_sample(now, ce, pe, gc)


def setc(total, now):
    fc.mark[ce_k] = total / 2
    fc.mark[pe_k] = total / 2
    bk.on_poll(now, gc)


# candle 09:30: 60 -> 62 -> 55 close (red), then 09:35 falls, fills, 09:40 above vwap exits
setc(60, D(2026, 9, 16, 9, 31)); setc(62, D(2026, 9, 16, 9, 32)); setc(55, D(2026, 9, 16, 9, 34, 50))
setc(55, D(2026, 9, 16, 9, 35, 1))
check("armed after red close", bk.trigger.pending is not None)
trig = bk.trigger.pending["trigger"]
setc(trig - 0.2, D(2026, 9, 16, 9, 36))
check("book entered", bk.pos is not None and bk.pos["trigger"] == trig)
setc(trig, D(2026, 9, 16, 9, 39, 59))
setc(70, D(2026, 9, 16, 9, 40, 1))                # 09:35 candle closes (entry candle: hold)
setc(70, D(2026, 9, 16, 9, 44, 59))
setc(70, D(2026, 9, 16, 9, 45, 1))                # 09:40 candle closes 70 > vwap -> exit
check("book exited", bk.pos is None and len(bk.trades) == 1)
t = bk.trades[0] if bk.trades else {}
check("trade pnl sign (loss)", t.get("net_usd", 0) < 0)
check("fees charged", t.get("fees", 0) > 0)

# restart resumes
bk2 = VwapBook("ist_day", params, tmp, log=lambda m: None)
bk2.on_poll(D(2026, 9, 16, 9, 46), gc)
check("resume same cycle", bk2.active and len(bk2.trades) == 1 and len(bk2.builder.closed) >= 3)

# square-off with an open position
bk2.on_sample(D(2026, 9, 16, 16, 0), 20, 20, gc)          # closes the stale candle first
fire(bk2, D(2026, 9, 16, 16, 0, 1))
check("entered before square-off", bk2.pos is not None)
bk2.on_poll(D(2026, 9, 16, 17, 10, 1), gc)
check("square-off flattened + cycle ended", not bk2.active)
cyc = [json.loads(l) for l in (tmp / "data" / "vwap_results" / "cycles.jsonl").read_text().splitlines()]
check("cycle summary written", cyc and cyc[-1]["trades"] == 2 and not cyc[-1]["unclosed"])

# MTM stop
bk3 = VwapBook("ist_day", params, Path(tempfile.mkdtemp()), log=lambda m: None)
fc.mark = dict(marks)
bk3.on_poll(D(2026, 9, 16, 10, 0, 1), gc)
check("late start flagged", bk3.late_start)
bk3.on_sample(D(2026, 9, 16, 10, 6), 30, 30, gc)
fire(bk3, D(2026, 9, 16, 10, 6, 1))
fc.mark[(bk3.pair["ce"], "CE")] = 70
fc.mark[(bk3.pair["pe"], "PE")] = 70
bk3.on_poll(D(2026, 9, 16, 10, 7), gc)
check("MTM stop flattens and stops", bk3.pos is None and bk3.trigger.done)

bk4 = VwapBook("ist_day", params, Path(tempfile.mkdtemp()), log=lambda m: None)
fc.mark = dict(marks); fc.ask = {}
bk4.on_poll(D(2026, 9, 16, 11, 0, 1), gc)
bk4.on_sample(D(2026, 9, 16, 11, 6), 5, 5, gc)
fire(bk4, D(2026, 9, 16, 11, 6, 1))
fc.ask = {(float(bk4.pair["ce"]), "CE"): 6.0, (float(bk4.pair["pe"]), "PE"): 6.0}
bk4.on_sample(D(2026, 9, 16, 11, 6, 2), 5, 5, gc)
check("MTM values open legs at buy-back asks",
      bk4.pos and abs(bk4.unrealized(fc) - ((bk4.pos["entry_combined"] - 12) - bk4.pos["entry_fees"])) < 1e-3)

# bad-fill guard: the book can only fill far below the trigger -> sell nothing
gp = {"versions": {"ist_day": dict(IST, mtm_stop_usd=0)}, "trigger_offset": 1.0,
      "candle_minutes": 5, "max_entry_slippage_pct": 5,
      "fees": {"taker_rate_notional": 0.0001, "premium_cap_rate": 0.035}}
bk5 = VwapBook("ist_day", gp, Path(tempfile.mkdtemp()), log=lambda m: None)
fc.mark = dict(marks); fc.ask = {}
bk5.on_poll(D(2026, 9, 16, 12, 0, 1), gc)
bk5.on_sample(D(2026, 9, 16, 12, 6), 50, 50, gc)
bk5.trigger.pending = {"trigger": 100.0, "signal": "x"}
fc.mark.update({(bk5.pair["ce"], "CE"): 30.0, (bk5.pair["pe"], "PE"): 30.0})   # fill 59 vs 100
bk5.on_sample(D(2026, 9, 16, 12, 6, 1), 30, 30, gc)
check("bad fill sells nothing", bk5.pos is None)
check("bad fill keeps the signal armed",
      bk5.trigger.pending and bk5.trigger.pending["trigger"] == 100.0)
check("bad fill costs no entry", bk5.trigger.entries == 0)
fc.mark.update({(bk5.pair["ce"], "CE"): 49.0, (bk5.pair["pe"], "PE"): 49.0})   # fill 97 vs 100
bk5.on_sample(D(2026, 9, 16, 12, 6, 2), 49, 49, gc)
check("fill within tolerance is taken", bk5.pos is not None and bk5.trigger.entries == 1)

# mtm_stop_usd 0 = no dollar stop at all
fc.mark.update({(bk5.pair["ce"], "CE"): 500.0, (bk5.pair["pe"], "PE"): 500.0})
bk5.on_poll(D(2026, 9, 16, 12, 7), gc)
check("no MTM stop when disabled",
      bk5.pos is not None and not bk5.trigger.done and bk5.mtm(fc) < -100)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
