"""
live/dry_run.py — offline test harness for the controller state machine.
========================================================================

A month of paper is the experiment; this is what makes it worth running. The
state machine decides real money later, and it will spend most of that month
unattended at 03:00, so every branch that matters is driven here against a
synthetic chain and a scripted clock — no network, no exchange, no waiting.

What is pinned:
  - the two hard invariants (never a short without a stop, never a half strangle)
  - entry at target, on the listed grid, never ATM
  - the 2x adjustment, and that it fires ONLY inside a window
  - stop-outs: one leg, then both
  - the A/B vs C temperament split, which is the whole experiment
  - max loss, square-off, cycle rollover across midnight
  - fees and spread crossing actually reaching the P&L

Run:  .venv/Scripts/python.exe live_trading_options/delta_btc/live/dry_run.py
"""

import sys
import json
import math
import shutil
import tempfile
import datetime as dt
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.sessions import SessionProfile
from core.selector import CE, PE
from live.controller import BTCController

PASS = FAIL = 0
FAILURES = []


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")
    return got == want


def ok(name, cond):
    return check(name, bool(cond), True)


# ── a synthetic chain that behaves like the real one ──────────────────────
class FakeChain:
    """Implements exactly the LiveChain surface the controller touches.

    Premiums decay away from the money on a listed grid that WIDENS with distance
    (100 near ATM, then 200, then 500) — the real shape of Delta's daily BTC
    chain, and the shape that broke the old fixed-interval selector.
    """

    def __init__(self, spot=80000.0, scale=600.0, atm_prem=400.0, expiry="050926"):
        self.expiry = expiry
        self.span = 20
        self.contract_value = 0.001
        self.strikes = self._grid()
        self.spot = None
        self.atm = None
        self.mark, self.bid, self.ask = {}, {}, {}
        self.scale, self.atm_prem = scale, atm_prem
        self.spread_pts = 2.0
        self.hide_side = None          # (strike, type, "bid"|"ask") -> empty book
        self.set_spot(spot)

    def _grid(self):
        s = []
        base = 80000
        for d in range(0, 2001, 100):          # dense near the money
            s += [base - d, base + d]
        for d in range(2200, 5001, 200):
            s += [base - d, base + d]
        for d in range(5500, 15001, 500):
            s += [base - d, base + d]
        return sorted(set(float(x) for x in s))

    def set_spot(self, spot: float):
        self.spot = float(spot)
        self.atm = min(self.strikes, key=lambda s: (abs(s - self.spot), s))
        self.mark, self.bid, self.ask = {}, {}, {}
        for s in self.strikes:
            for t in (CE, PE):
                otm = (s - self.spot) if t == CE else (self.spot - s)
                # rich at the money, decaying outward; intrinsic when in the money
                prem = self.atm_prem * math.exp(-max(otm, 0.0) / self.scale)
                if otm < 0:
                    prem += -otm
                prem = round(max(prem, 0.1), 1)
                self.mark[(s, t)] = prem
                self.bid[(s, t)] = round(max(prem - self.spread_pts / 2, 0.1), 1)
                self.ask[(s, t)] = round(prem + self.spread_pts / 2, 1)
        if self.hide_side:
            st, tp, which = self.hide_side
            (self.bid if which == "bid" else self.ask).pop((st, tp), None)

    # ── LiveChain surface ────────────────────────────────────────────────
    def _band(self):
        i = self.strikes.index(self.atm)
        return (self.strikes[max(0, i - self.span)],
                self.strikes[min(len(self.strikes) - 1, i + self.span)])

    def chain(self):
        lo, hi = self._band()
        return {k: v for k, v in self.mark.items() if lo <= k[0] <= hi}

    def contract(self, strike, opt_type):
        strike = float(strike)
        if strike not in self.strikes:
            return None
        code = "C" if opt_type == CE else "P"
        return {"symbol": f"{code}-BTC-{strike:.0f}-{self.expiry}",
                "product_id": int(strike), "contract_value": self.contract_value,
                "tick_size": 0.1}

    def fill_price(self, strike, opt_type, side):
        key = (float(strike), opt_type)
        book = self.bid if side == "SELL" else self.ask
        p = book.get(key)
        if p is not None:
            return p, True
        return self.mark.get(key), False

    def is_ready(self):
        return self.spot is not None and self.atm is not None

    def seconds_to_settlement(self, now):
        return 3600


PROFILE_CFG = {
    "entry_time": "09:30", "square_off": "17:10",
    "target_premium": 70, "sl_premium": 140, "contracts": 100,
    "ends_on_both_stopped": True, "ends_on_max_loss": True,
    "max_fresh_entries": 3,
}
PARAMS = {"adjust_trigger_ratio": 2.0, "max_loss_usd": {"t": 45},
          "fees": {"taker_rate_notional": 0.0001, "premium_cap_rate": 0.035},
          "slippage": {"cross_the_spread": True}}


# Every controller built here writes into a THROWAWAY directory. Without this the
# harness appends its synthetic cycles to data/results/cycles.jsonl — the real
# month's dataset — and silently corrupts the thing the experiment is judged on.
SANDBOX = Path(tempfile.mkdtemp(prefix="btc_dryrun_"))


def make(profile_over=None, params_over=None, name="t"):
    cfg = dict(PROFILE_CFG)
    cfg.update(profile_over or {})
    p = SessionProfile(name, cfg)
    prm = json.loads(json.dumps(PARAMS))
    prm["max_loss_usd"] = {name: PARAMS["max_loss_usd"].get("t", 45)}
    for k, v in (params_over or {}).items():
        prm[k] = v
    return BTCController(p, prm, out_dir=SANDBOX)


def T(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def run(ctrl, chain, times, invariant=True):
    """Step the controller through a list of timestamps, checking the hard
    invariant after every single one."""
    for t in times:
        ctrl.on_tick(chain, T(t) if isinstance(t, str) else t)
        if invariant and ctrl.position.unprotected_legs():
            check(f"INVARIANT never-naked at {t}",
                  [l.symbol for l in ctrl.position.unprotected_legs()], [])


def minutes(start, end, step=1):
    a, b = T(start), T(end)
    out = []
    while a <= b:
        out.append(a)
        a += dt.timedelta(minutes=step)
    return out


D = "2026-09-05"

# ══ 1. a clean cycle ══════════════════════════════════════════════════════
c = make()
ch = FakeChain(spot=80000.0)
run(c, ch, [f"{D} 09:29:00", f"{D} 09:30:00"])
ok("entry fired at 09:30", c.entered)
ok("both legs live after entry", c.position.is_complete)
ok("both legs protected", not c.position.unprotected_legs())
check("CE leg is above ATM", c.position.ce.strike > ch.atm, True)
check("PE leg is below ATM", c.position.pe.strike < ch.atm, True)
check("neither leg is ATM",
      ch.atm not in (c.position.ce.strike, c.position.pe.strike), True)
check("CE stop is the profile SL", c.position.ce.sl_trigger, 140.0)
ok("entry premium is near the $70 target",
   all(40 <= l.entry_price <= 110 for l in (c.position.ce, c.position.pe)))
ok("a paper stop is never claimed as resting at the exchange",
   not c.position.ce.sl_at_broker)

# a SELL must fill at the bid, below the mark
ce = c.position.ce
check("sell filled at the bid, not the mark", ce.entry_price, ch.bid[(ce.strike, CE)])
ok("fill is flagged as having crossed a real book", ce.entry_crossed)
ok("entry fee was charged", ce.fees > 0)

# ══ 2. no adjustment outside a window ═════════════════════════════════════
c2 = make()
ch2 = FakeChain(spot=80000.0)
run(c2, ch2, [f"{D} 09:30:00"])
before = (c2.position.ce.strike, c2.position.pe.strike)
# +300 lifts the CE to ~114 and drops the PE to ~42: past the 2x ratio, but both
# still short of the 140 stop, so the imbalance survives to be adjusted rather
# than being resolved by a stop-out
ch2.set_spot(80300.0)
run(c2, ch2, [f"{D} 09:52:00", f"{D} 09:58:30"])   # both outside any window
ok("the move created a 2x imbalance without breaching either stop",
   c2.position.is_complete)
check("no adjustment outside a window",
      (c2.position.ce.strike, c2.position.pe.strike), before)

# ...and it DOES adjust once a window opens
run(c2, ch2, [f"{D} 10:00:00"])
ok("adjusted inside the 10:00 window",
   (c2.position.ce.strike, c2.position.pe.strike) != before)
ok("still a complete strangle after the adjustment", c2.position.is_complete)
ok("the replaced leg went to history", len(c2.position.history) >= 1)
ok("the replaced leg is CLOSED, not lost",
   all(l.status in ("CLOSED", "STOPPED") for l in c2.position.history))
ok("adjustment closed the SMALLER leg",
   c2.position.history[0].exit_reason.startswith("adjustment"))

# a window fires only ONCE even though ticks keep arriving inside it
n_hist = len(c2.position.history)
run(c2, ch2, [f"{D} 10:00:20", f"{D} 10:00:45", f"{D} 10:00:59"])
check("a window acts exactly once", len(c2.position.history), n_hist)

# ══ 3. one leg stopped out, then re-entered ═══════════════════════════════
c3 = make()
ch3 = FakeChain(spot=80000.0)
run(c3, ch3, [f"{D} 09:30:00"])
ce_strike = c3.position.ce.strike
# Leave the CE 600 points OTM: premium ~147, just through the 140 stop. A far
# larger move would breach max loss as well, and then this would be testing the
# loss limit rather than the stop — the two paths close the position by different
# mechanisms and are tested separately on purpose.
ch3.set_spot(ce_strike - 600)
run(c3, ch3, [f"{D} 09:40:00"])
ok("CE was stopped out", c3.position.ce is None)
ok("position is single-legged", c3.position.is_single)
check("the stopped leg is recorded as STOPPED", c3.position.history[-1].status, "STOPPED")
ok("single-legged running is allowed between windows", not c3.killed)
ok("stop fill is at or beyond the trigger",
   c3.position.history[-1].exit_price >= 140.0)
ok("one stop-out alone does not breach max loss", abs(c3.mtm) < 45)
run(c3, ch3, [f"{D} 09:45:00"])
ok("the missing side was re-entered at the next window", c3.position.is_complete)

# ══ 4. BOTH legs stopped — the A/B vs C split ═════════════════════════════
def both_stopped(ctrl):
    """Stop both legs one after the other, each with the gentlest move that does
    the job, so the cycle ends on the STOPS and not on the loss limit."""
    ch = FakeChain(spot=80000.0)
    ctrl.on_tick(ch, T(f"{D} 09:30:00"))
    ce_s = ctrl.position.ce.strike
    pe_s = ctrl.position.pe.strike
    ch.set_spot(ce_s - 600)                 # CE ~147 -> through its 140 stop
    ctrl.on_tick(ch, T(f"{D} 09:40:00"))
    ch.set_spot(pe_s + 600)                 # PE ~147 -> through its 140 stop
    ctrl.on_tick(ch, T(f"{D} 09:41:00"))
    return ch

cA = make()
chA = both_stopped(cA)
ok("A: both legs stopped leaves the book flat", cA.position.is_flat)
ok("A: the cycle ends after a double stop-out", cA.killed)
check("A: the reason is recorded", cA.kill_reason, "both legs stopped out")
run(cA, chA, [f"{D} 10:00:00", f"{D} 10:15:00"])
ok("A: does NOT re-enter after ending", cA.position.is_flat)

cC = make({"ends_on_both_stopped": False, "ends_on_max_loss": False,
           "max_fresh_entries": 8}, name="cont")
chC = both_stopped(cC)
ok("C: both legs stopped also leaves the book flat", cC.position.is_flat)
ok("C: the cycle does NOT end", not cC.killed)
chC.set_spot(80000.0)
run(cC, chC, [f"{D} 10:00:00"])
ok("C: re-enters at the next window after a double stop-out",
   cC.position.is_complete)
check("C: that counted as a second fresh entry", cC.fresh_entries, 2)

# the fresh-entry cap still binds
cCap = make({"ends_on_both_stopped": False, "max_fresh_entries": 1}, name="cap")
chCap = both_stopped(cCap)
chCap.set_spot(80000.0)
run(cCap, chCap, [f"{D} 10:00:00"])
ok("the fresh-entry cap blocks a re-open", cCap.position.is_flat)

# ══ 5. max loss ═══════════════════════════════════════════════════════════
# With stops at 2x the target the two legs can only lose (140-70)*0.1*2 = $14
# between them, so an ORDERLY pair of stop-outs can never reach the $45 limit.
# That is the intended relationship — the limit sits above the worst case so the
# per-leg stops govern the cycle. It is therefore reachable only by a GAP: a
# poll-to-poll jump that fills a stop far beyond its trigger. That is what this
# simulates, and it is the only realistic way this branch ever runs.
cM = make()
chM = FakeChain(spot=80000.0)
run(cM, chM, [f"{D} 09:30:00"])
ce_leg = cM.position.ce
chM.mark[(ce_leg.strike, CE)] = 600.0      # gapped straight through the 140 stop
chM.bid[(ce_leg.strike, CE)] = 599.0
chM.ask[(ce_leg.strike, CE)] = 601.0
run(cM, chM, [f"{D} 09:35:00"])
ok("the gapped leg was stopped, filled where the market actually is",
   any(l.status == "STOPPED" and l.exit_price >= 600.0 for l in cM.position.history))
ok("max loss ended the cycle for A", cM.killed)
check("max loss is the recorded reason", "max loss" in (cM.kill_reason or ""), True)
ok("max loss flattened the book", cM.position.is_flat)

cMC = make({"ends_on_max_loss": False, "ends_on_both_stopped": False,
            "max_fresh_entries": 8}, name="contm")
chMC = FakeChain(spot=80000.0)
run(cMC, chMC, [f"{D} 09:30:00"])
ce_leg2 = cMC.position.ce
chMC.mark[(ce_leg2.strike, CE)] = 600.0
chMC.bid[(ce_leg2.strike, CE)] = 599.0
chMC.ask[(ce_leg2.strike, CE)] = 601.0
run(cMC, chMC, [f"{D} 09:35:00"])
ok("C: max loss flattens but does NOT end the cycle", not cMC.killed)
ok("C: book is flat after the max-loss flatten", cMC.position.is_flat)
chMC.set_spot(80000.0)
run(cMC, chMC, [f"{D} 09:45:00"])
ok("C: goes short again at the next window after a max-loss flatten",
   cMC.position.is_complete)

# ══ 6. square-off ═════════════════════════════════════════════════════════
cS = make()
chS = FakeChain(spot=80000.0)
run(cS, chS, [f"{D} 09:30:00"])
ok("holding a position before square-off", cS.position.is_complete)
run(cS, chS, [f"{D} 17:09:00"])
ok("still holding one minute before square-off", cS.position.is_complete)
run(cS, chS, [f"{D} 17:10:00"])
ok("flat after square-off", cS.position.is_flat)
# The book is RESET at the cycle boundary, so the completed-cycle record is the
# only place the day's legs survive — which is exactly why it carries the exit
# reasons. Reading position.history here would read the NEXT cycle's empty book
# and pass vacuously, which is how the dropped-position bug hid in the first place.
check("square-off recorded exactly one completed cycle", len(cS.cycles_done), 1)
rec = cS.cycles_done[0]
check("both legs are in the record", rec["legs"], 2)
check("both legs were closed BY the square-off",
      rec["exit_reasons"], ["square-off", "square-off"])
ok("nothing was left stuck at the boundary", rec["left_stuck"] is None)
ok("the cycle was not ended early", not rec["ended_early"])
ok("the square-off P&L is real, not a dropped position", rec["realized"] != 0)

# ══ 7. atomic entry when a leg cannot fill ════════════════════════════════
cN = make()
chN = FakeChain(spot=80000.0)
# blank BOTH sides of the book for the PE the selector will want, and its mark
target_pe = None
from core.selector import select_entry_leg
target_pe = select_entry_leg(chN.chain(), chN.atm, PE, target=70)["strike"]
chN.bid.pop((target_pe, PE), None)
chN.ask.pop((target_pe, PE), None)
chN.mark.pop((target_pe, PE), None)
run(cN, chN, [f"{D} 09:30:00"])
ok("a leg with no price at all never leaves a half strangle",
   cN.position.is_flat or cN.position.is_complete)
ok("no unprotected leg was left behind", not cN.position.unprotected_legs())

# ══ 8. cycle rollover across midnight (profile B) ═════════════════════════
cB = make({"entry_time": "17:35", "square_off": "17:10"}, name="b")
chB = FakeChain(spot=80000.0)
run(cB, chB, ["2026-09-05 17:35:00"])
ok("B entered at 17:35", cB.entered)
first_cycle = cB.cycle
run(cB, chB, ["2026-09-05 23:00:00", "2026-09-06 02:00:00", "2026-09-06 09:00:00"])
check("B keeps ONE cycle across midnight", cB.cycle, first_cycle)
ok("B still holds its position overnight", cB.position.is_complete)
run(cB, chB, ["2026-09-06 17:10:00"])
ok("B flat after the next day's square-off", cB.position.is_flat)
run(cB, chB, ["2026-09-06 17:20:00"])          # the settlement gap
check("B cycle key is None in the settlement gap", cB.cycle, None)
check("B recorded exactly one completed cycle", len(cB.cycles_done), 1)
run(cB, chB, ["2026-09-06 17:35:00"])
ok("B opened a NEW cycle the next evening", cB.cycle != first_cycle and cB.entered)

# ══ 9. money: fees and the spread actually reach P&L ══════════════════════
cF = make()
chF = FakeChain(spot=80000.0)
run(cF, chF, [f"{D} 09:30:00"])
leg = cF.position.ce
gross = leg.gross_pnl(leg.entry_price)
check("gross P&L at the entry price is zero", gross, 0.0)
ok("net P&L is worse than gross by exactly the fees",
   abs(leg.pnl(leg.entry_price) - (gross - leg.fees)) < 1e-9)
# multiplier: 100 contracts x 0.001 BTC = 0.1 per point of premium
check("leg multiplier is contracts x contract value", leg.multiplier, 0.1)
check("a 10-point fall in premium is $1.00 gross",
      round(leg.gross_pnl(leg.entry_price - 10), 4), 1.0)
run(cF, chF, [f"{D} 17:10:00"])
ok("round-trip fees are charged on both sides",
   all(l.fees > 0 for l in cF.position.history))
recF = cF.cycles_done[0]
ok("the cycle record carries gross and fees separately", recF["fees"] > 0)
ok("cycle realized P&L is exactly gross minus fees",
   abs(recF["realized"] - (recF["gross"] - recF["fees"])) < 1e-6)
ok("net is worse than gross — fees are a cost, not a credit",
   recF["realized"] < recF["gross"])
# Holding a strangle flat from entry to square-off still LOSES, because both legs
# were sold at the bid and bought back at the ask. That is the spread being paid
# twice, and it is the single biggest reason a paper book that filled at the mark
# would have lied about this strategy.
ok("a flat round trip costs the spread on both legs", recF["gross"] < 0)

# ══ 9b. surviving a restart mid-cycle ═════════════════════════════════════
# B and C hold for 23.6h, so a restart inside a cycle is not an edge case over a
# month — it is a certainty. What must survive: the open legs, the windows already
# acted on, the fresh-entry count, and the cycles already banked.
cR = make({"entry_time": "17:35", "square_off": "17:10"}, name="resume")
chR = FakeChain(spot=80000.0)
run(cR, chR, ["2026-09-05 17:35:00", "2026-09-05 18:00:00", "2026-09-05 20:00:00"])
ok("pre-restart: holding a strangle", cR.position.is_complete)
before = {
    "cycle": cR.cycle,
    "ce": cR.position.ce.strike, "pe": cR.position.pe.strike,
    "ce_entry": cR.position.ce.entry_price,
    "windows": len(cR.done_windows), "fresh": cR.fresh_entries,
}

# a brand-new controller, as though the process had been killed and restarted
cR2 = make({"entry_time": "17:35", "square_off": "17:10"}, name="resume")
ok("a fresh controller starts empty", cR2.position.is_flat)
restored = cR2.restore(T("2026-09-05 20:05:00"))
ok("restore reported success", restored)
check("restored the same cycle", cR2.cycle, before["cycle"])
check("restored the CE leg", cR2.position.ce.strike, before["ce"])
check("restored the PE leg", cR2.position.pe.strike, before["pe"])
check("restored the CE entry price", cR2.position.ce.entry_price, before["ce_entry"])
check("restored the windows already acted on", len(cR2.done_windows), before["windows"])
check("restored the fresh-entry count", cR2.fresh_entries, before["fresh"])
ok("restored legs are still protected", not cR2.position.unprotected_legs())
ok("restored legs count as live", cR2.position.is_complete)
ok("restored cycle is not marked late-start", not cR2.late_start)

# and it carries on normally from there, ending flat at the square-off
run(cR2, chR, ["2026-09-06 09:00:00", "2026-09-06 17:10:00"])
ok("resumed cycle squares off normally", cR2.position.is_flat)
check("resumed cycle was recorded once", len(cR2.cycles_done), 1)
ok("the resumed cycle is NOT flagged late-start",
   not cR2.cycles_done[0]["late_start"])

# a STALE resume file must never resurrect a position into a different cycle
cR3 = make({"entry_time": "17:35", "square_off": "17:10"}, name="resume")
ok("a resume file from another cycle is refused",
   not cR3.restore(T("2026-09-12 20:00:00")))
ok("nothing was adopted from the stale file", cR3.position.is_flat)

# ...and no resume file at all is simply a fresh start, not an error
cR4 = make(name="never_saved")
ok("no resume file is a clean fresh start", not cR4.restore(T(f"{D} 12:00:00")))

# ══ 10. the never-ATM rule holds across many spots ════════════════════════
for spot in (79950.0, 80000.0, 80049.0, 80051.0, 81234.0, 78777.0):
    cx = make()
    chx = FakeChain(spot=spot)
    run(cx, chx, [f"{D} 09:30:00"])
    if cx.position.is_complete:
        ok(f"never sells ATM at spot {spot:.0f}",
           chx.atm not in (cx.position.ce.strike, cx.position.pe.strike))
        ok(f"CE above / PE below ATM at spot {spot:.0f}",
           cx.position.ce.strike > chx.atm > cx.position.pe.strike)

# ══ 11. a full day, minute by minute, invariant checked every step ════════
cD = make()
chD = FakeChain(spot=80000.0)
path = [80000, 80400, 80100, 79600, 79900, 80800, 81300, 80600, 80200, 79400]
allmins = minutes(f"{D} 09:29:00", f"{D} 17:12:00", step=1)
for i, t in enumerate(allmins):
    if i % 46 == 0:
        chD.set_spot(float(path[(i // 46) % len(path)]))
    cD.on_tick(chD, t)
    if cD.position.unprotected_legs():
        check(f"INVARIANT never-naked during the full day at {t:%H:%M}",
              [l.symbol for l in cD.position.unprotected_legs()], [])
ok("full day ends flat", cD.position.is_flat)
ok("full day recorded a cycle", len(cD.cycles_done) == 1)
recD = cD.cycles_done[0]
ok("full day traded something", recD["legs"] >= 2)
ok("no cover was left stuck", not cD.stuck and recD["left_stuck"] is None)
ok("every leg in the record has an exit reason", all(r for r in recD["exit_reasons"]))
ok("the last leg out was the square-off, not a dropped position",
   recD["ended_early"] or "square-off" in recD["exit_reasons"])
print(f"  [full-day walk] {recD['legs']} legs, realized ${recD['realized']:.2f}, "
      f"fees ${recD['fees']:.2f}, adjustments {recD['adjustments']}, "
      f"stopped {recD['stopped_legs']}")

# ── report ────────────────────────────────────────────────────────────────
print(f"\n  {PASS} passed, {FAIL} failed")
for f in FAILURES:
    print(f"   FAIL {f}")
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if FAIL else 0)
