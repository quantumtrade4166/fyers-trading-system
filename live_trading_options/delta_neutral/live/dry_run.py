"""
live/dry_run.py — offline rehearsal of the full state machine.
==============================================================

The VWAP strangle's worst bug ("orders never fired") could only be found live,
because nothing exercised the decision path outside market hours. This runs the
real `DNController` — the same code that will place real orders — against a
scripted chain and a scripted clock, in paper mode, and asserts what it did.

Everything here is deterministic and offline: no broker, no socket, no Fyers.
Run it after ANY change to the controller, the selector or the windows.

    python live_trading_options/delta_neutral/live/dry_run.py
"""

import sys
import json
import datetime as dt
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from live.controller import DNController
from core.selector import CE, PE

PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text())
DATE = "2026-08-25"
D = dt.date(2026, 8, 25)
ATM, IV = 24_250, 50

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"    ok   {name}")
    else:
        FAIL += 1
        print(f"    FAIL {name}: got {got!r}, want {want!r}")


def at(hms: str) -> dt.datetime:
    h, m, s = (list(map(int, hms.split(":"))) + [0])[:3]
    return dt.datetime.combine(D, dt.time(h, m, s))


def chain_of(ce_prems: dict, pe_prems: dict, atm: int = ATM) -> dict:
    """{offset_in_strikes: premium} -> a real chain dict. Offset 1 = OTM1."""
    c = {(atm, CE): 60.0, (atm, PE): 60.0}          # ATM always present, never sellable
    for n, p in ce_prems.items():
        c[(atm + n * IV, CE)] = p
    for n, p in pe_prems.items():
        c[(atm - n * IV, PE)] = p
    return c


def new_ctrl(dte: int = 0, index: str = "NIFTY", control: dict = None) -> DNController:
    """A controller wired for paper: no kite client, fake Fyers symbols.

    The control channel is STUBBED. Without this the controller reads the real
    live_control_{index}.json and a qty/max-loss left there from a previous
    session silently overrides whatever a scenario sets up — a test that reads
    production state is a test that fails for the wrong reason. Pass `control` to
    exercise the channel deliberately.
    """
    ctrl = DNController(index, DATE, D, dte, params=PARAMS,
                        lot_size=PARAMS["lot_sizes"][index], kite=None,
                        symbol_lookup=lambda strike, t: f"NSE:{index}{strike}{t}")
    ctrl.persist = lambda: None                     # keep the test off the filesystem
    ctrl._write_tick = lambda: None
    if control is None:
        ctrl._check_control = lambda: None
    else:
        import live.controller as _cm
        _cm.read_control = lambda idx: control      # deterministic, in-memory
    return ctrl


def step(ctrl, chain, when, spot=ATM, times=1):
    for _ in range(times):
        ctrl.on_tick(chain, spot, at(when))


def evs(ctrl, etype):
    return [e for e in ctrl.events if e["type"] == etype]


# ══ 1. a normal entry ════════════════════════════════════════════════════
print("\n  [1] 09:30 entry — Nifty 0-DTE, target 20, preferred band 15-20, SL 40")
c = new_ctrl(dte=0)
# CE ladder 30/22/16/11, PE ladder 28/21/15/10  →  band 15-20 picks 16 / 15
ch = chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10})
step(c, ch, "09:29:50")
check("nothing before 09:30", c.position.is_flat, True)

step(c, ch, "09:30:02")
check("both legs open", c.position.is_complete, True)
check("CE strike from the preferred band", c.position.ce.strike, ATM + 3 * IV)
check("PE strike from the preferred band", c.position.pe.strike, ATM - 3 * IV)
check("CE never ATM", c.position.ce.strike != ATM, True)
check("CE has a confirmed stop", c.position.ce.is_protected, True)
check("PE has a confirmed stop", c.position.pe.is_protected, True)
check("stop trigger is the table value", c.position.ce.sl_trigger, 40)
# Zerodha blocks SL-M on options, so the stop is trigger + limit. The gap is what
# makes it fill instead of being jumped by the move it exists for.
check("stop limit is trigger + buffer (40 -> 42)", c.position.ce.sl_limit, 42.0)
# PAPER must never claim a broker stop — that is what would mislead you on screen
check("paper stop is NOT at the broker", c.position.ce.sl_at_broker, False)
check("paper stop still counts as protected", c.position.ce.sl_verified, True)
check("no unprotected leg", c.position.unprotected_legs(), [])
check("qty is 1 lot", c.position.ce.qty, 65)

step(c, ch, "09:30:20", times=5)
check("entry fires exactly once", len(evs(c, "entry_complete")), 1)
# fills are stamped with the STRATEGY's clock, not the wall clock — otherwise a
# paper run labels every trade with the moment the script happened to run
check("entry time is the strategy clock", c.position.ce.entry_time, "09:30:02")

# ══ 2. windows fire only inside the minute, and only once ════════════════
print("\n  [2] adjustment windows — timing")
before = len(c.events)
step(c, ch, "09:44:59", times=3)
check("nothing at 09:44:59", len(c.events), before)
step(c, ch, "09:46:30", times=3)
check("nothing at 09:46:30", len(c.events), before)

step(c, ch, "09:45:10")
check("09:45 window evaluated", len(evs(c, "window_checked")), 1)
check("balanced legs → no adjustment", len(evs(c, "adjust_triggered")), 0)
step(c, ch, "09:45:30", times=10)
check("window evaluated exactly once despite many ticks",
      len(evs(c, "window_checked")), 1)

# ══ 3. the 2x rule ═══════════════════════════════════════════════════════
print("\n  [3] 10:00 window — CE 30 vs PE 12 triggers, PE is replaced")
# Spot has risen. CE (24400) is 30 — still UNDER its 40 stop, so it is the 2x rule
# that acts here, not the stop. PE (24100) is 12, and 30 >= 2*12, so PE is replaced
# by the highest strike below 30 (and above 30/2 = 15): PE 24200 at 28.
ch2 = chain_of({1: 60, 2: 45, 3: 30, 4: 20},
               {1: 28, 2: 20, 3: 12, 4: 8})
step(c, ch2, "10:00:05")
check("adjustment triggered", len(evs(c, "adjust_triggered")), 1)
check("the SMALLER leg was replaced", evs(c, "adjust_triggered")[0]["replace"], PE)
check("still a complete strangle", c.position.is_complete, True)
check("new PE is the strike just below the open CE", c.position.pe.strike, ATM - 1 * IV)
check("new PE premium is below the open CE", c.position.pe.entry_price < 30, True)
check("new PE premium is above open/2 (not born imbalanced)",
      c.position.pe.entry_price > 15, True)
check("new PE got its own stop", c.position.pe.is_protected, True)
check("replaced leg moved to history", len(c.position.history), 1)
check("no unprotected leg after adjusting", c.position.unprotected_legs(), [])
# the closed-position row the terminal renders
closed = c.position.history[0]
check("closed leg keeps its entry price", closed.entry_price, 15.0)
check("closed leg records its exit", closed.exit_price, 12.0)
check("closed leg P&L = (entry-exit)*qty", closed.pnl(), round((15.0 - 12.0) * 65, 2))
check("closed leg says what closed it", closed.exit_reason, "adjustment 10:00 — 2x rule")
check("closed leg exit time is the strategy clock", closed.exit_time, "10:00:05")

# ══ 4. a stop-out leaves ONE leg, re-entered at the next window ══════════
print("\n  [4] stop-out → single leg → re-entry at the NEXT window")
c2 = new_ctrl(dte=0)
step(c2, chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10}), "09:30:02")
check("started complete", c2.position.is_complete, True)
ce_strike = c2.position.ce.strike

# CE runs to 42 — through its 40 stop
hot = chain_of({1: 70, 2: 55, 3: 42, 4: 30, 5: 20, 6: 10},
               {1: 22, 2: 16, 3: 12, 4: 8})
step(c2, hot, "10:05:00")
check("stop fired on CE", len(evs(c2, "stop_hit")), 1)
check("now single-legged", c2.position.is_single, True)
check("the missing side is CE", c2.position.missing_side(), CE)
check("no re-entry outside a window", c2.position.is_complete, False)
check("surviving PE still protected", c2.position.pe.is_protected, True)

step(c2, hot, "10:15:05")
check("re-entered at the 10:15 window", c2.position.is_complete, True)
check("re-entry is CE", len(evs(c2, "reentry")), 1)
check("new CE is below the open PE premium",
      c2.position.ce.entry_price < c2._mark(c2.position.pe), True)
check("new CE strike differs from the stopped one",
      c2.position.ce.strike != ce_strike, True)
check("new CE protected", c2.position.ce.is_protected, True)

# ══ 5. the skip rule (the spec's own example) ════════════════════════════
print("\n  [5] no valid strike → skip the whole window, position untouched")
c3 = new_ctrl(dte=0)
step(c3, chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10}), "09:30:02")
pe_before = c3.position.pe.strike
# CE = 21, and the best PE anywhere is 9  →  9 <= 21/2  →  skip
skewed = chain_of({1: 35, 2: 28, 3: 21, 4: 14}, {1: 9, 2: 6, 3: 4, 4: 3})
step(c3, skewed, "10:00:05")
check("window was skipped", len(evs(c3, "window_skipped")), 1)
check("nothing was closed", c3.position.is_complete, True)
check("PE leg untouched", c3.position.pe.strike, pe_before)
check("no adjustment happened", len(evs(c3, "adjust_triggered")), 0)

# ══ 6. max loss ══════════════════════════════════════════════════════════
print("\n  [6] max loss → square off everything and stop for the day")
c4 = new_ctrl(dte=0)
c4.max_loss = 1000
step(c4, chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10}), "09:30:02")
entry_comb = c4.position.ce.entry_price + c4.position.pe.entry_price
# push both legs up hard: 1 lot = 65 units, so ~+16 combined points = -1040
blow = chain_of({1: 90, 2: 80, 3: 39, 4: 30}, {1: 60, 2: 50, 3: 24, 4: 18})
step(c4, blow, "10:07:00")
check("killed on max loss", c4.killed, True)
check("kill reason recorded", "max loss" in (c4.kill_reason or ""), True)
check("flat after the kill", c4.position.is_flat, True)
check("trading is done for the day", c4.done, True)
step(c4, blow, "10:15:05")
check("no re-entry after a kill", c4.position.is_flat, True)

# ══ 7. 15:14 square-off ══════════════════════════════════════════════════
print("\n  [7] 15:14 — everything closed, no exceptions")
c5 = new_ctrl(dte=0)
calm = chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10})
step(c5, calm, "09:30:02")
step(c5, calm, "15:13:59")
check("still open at 15:13:59", c5.position.is_complete, True)
step(c5, calm, "15:14:00")
check("flat at 15:14:00", c5.position.is_flat, True)
check("square-off logged", len(evs(c5, "flatten")), 1)
check("reason is the square-off", evs(c5, "flatten")[0]["reason"], "15:14 square-off")
check("both legs in history", len(c5.position.history), 2)

# ══ 8. a no-trade day never trades ═══════════════════════════════════════
print("\n  [8] DTE 2 — chain only, never an order")
c6 = new_ctrl(dte=2)
check("trading not allowed on DTE 2", c6.trades_allowed, False)
step(c6, calm, "09:30:02")
step(c6, calm, "10:00:05")
check("still flat", c6.position.is_flat, True)
check("no events at all", c6.events, [])

# ══ 9. the protection invariant ══════════════════════════════════════════
print("\n  [9] a leg that cannot be protected is covered, never left naked")
c7 = new_ctrl(dte=0)
c7.executor.place_stop = lambda leg, trigger: (None, False, False, 0)   # every stop fails
step(c7, calm, "09:30:02")
check("entry aborted when the stop cannot be placed", c7.position.is_flat, True)
check("abort was logged", len(evs(c7, "entry_aborted")), 1)
check("no leg left short", c7.position.live_legs(), [])
check("no unprotected leg", c7.position.unprotected_legs(), [])

# ══ 10. the control channel: arm, resize, KILL ═══════════════════════════
print("\n [10] control channel — arm / resize / kill from the dashboard")
ctl = {"mode": "paper", "kill": False, "qty": 130, "mtm_stop": 4000}
c8 = new_ctrl(dte=0, control=ctl)
step(c8, calm, "09:29:00")
check("qty override applied while flat", c8.qty, 130)
check("max-loss override applied while flat", c8.max_loss, 4000.0)

step(c8, calm, "09:30:02")
check("entered at the overridden size", c8.position.ce.qty, 130)
ctl["qty"] = 195                                   # try to resize mid-position
c8._last_ctrl = 0
step(c8, calm, "09:31:00")
check("size change IGNORED while in a position", c8.qty, 130)

ctl["kill"] = True
c8._last_ctrl = 0
step(c8, calm, "09:32:00")
check("KILL closed the position", c8.position.is_flat, True)
check("KILL stopped the day", c8.done, True)
check("kill reason recorded", c8.kill_reason, "kill switch")

# arming flips the executor into live-order mode (no kite client here, so it
# stays paper in practice — this checks the flag path, not order placement)
c9 = new_ctrl(dte=0, control={"mode": "live", "kill": False, "qty": None, "mtm_stop": None})
step(c9, calm, "09:29:00")
check("mode read from the control file", c9.mode, "live")
check("no kite client → still cannot place real orders", c9.executor.is_live, False)

# ══ 11. restart recovery ═════════════════════════════════════════════════
print("\n [11] mid-day restart — the real position is recovered, not duplicated")


class FakeKite:
    """Just enough of a broker for reconcile_broker: our own tagged fills plus
    the stop orders still resting at the exchange."""
    def __init__(self, with_stops=True):
        self.with_stops = with_stops

    def orders(self):
        o = [{"tag": "dnstrangle", "status": "COMPLETE", "tradingsymbol": "NIFTY24450CE",
              "exchange": "NFO", "transaction_type": "SELL", "filled_quantity": 65,
              "average_price": 17.5, "order_id": "R1", "order_timestamp": "10:01:00",
              "order_type": "LIMIT"},
             {"tag": "dnstrangle", "status": "COMPLETE", "tradingsymbol": "NIFTY24050PE",
              "exchange": "NFO", "transaction_type": "SELL", "filled_quantity": 65,
              "average_price": 16.0, "order_id": "R2", "order_timestamp": "10:01:02",
              "order_type": "LIMIT"}]
        if self.with_stops:
            o += [{"tag": "dnstrangle", "status": "TRIGGER PENDING", "order_type": "SL",
                   "tradingsymbol": "NIFTY24450CE", "exchange": "NFO",
                   "trigger_price": 40, "quantity": 65, "order_id": "S1"},
                  {"tag": "dnstrangle", "status": "TRIGGER PENDING", "order_type": "SL",
                   "tradingsymbol": "NIFTY24050PE", "exchange": "NFO",
                   "trigger_price": 40, "quantity": 65, "order_id": "S2"}]
        return o

    def instruments(self, exchange):
        return [{"tradingsymbol": "NIFTY24450CE", "strike": 24450, "instrument_type": "CE",
                 "expiry": D, "lot_size": 65, "name": "NIFTY"},
                {"tradingsymbol": "NIFTY24050PE", "strike": 24050, "instrument_type": "PE",
                 "expiry": D, "lot_size": 65, "name": "NIFTY"}]


def restarted(with_stops=True):
    c = new_ctrl(dte=0)
    c.executor.kite = FakeKite(with_stops)
    c.reconcile_broker()
    return c


r1 = restarted()
check("position recovered from the broker", r1.position.is_complete, True)
check("CE strike recovered", r1.position.ce.strike, 24450)
check("PE strike recovered", r1.position.pe.strike, 24050)
check("real entry price recovered", r1.position.ce.entry_price, 17.5)
check("resting stop re-attached", r1.position.ce.sl_order_id, "S1")
check("recovered leg counts as protected", r1.position.ce.is_protected, True)
check("entry latched — no second 09:30 entry", r1.entered, True)

# the whole point: the next window must NOT open a duplicate strangle
step(r1, calm, "10:15:05")
check("next window did NOT open a second strangle", len(evs(r1, "entry_complete")), 0)
check("still exactly two legs", r1.position.n_live, 2)

# a recovered leg WITHOUT a resting stop must be protected or closed, never left
r2 = restarted(with_stops=False)
check("recovered leg with no stop is flagged unprotected",
      len(r2.position.unprotected_legs()), 2)
step(r2, calm, "10:16:00")
check("protection invariant resolved it", r2.position.unprotected_legs(), [])

# ══ 12. a stop that stops existing at the broker ═════════════════════════
print("\n [12] the broker's stop vanishes — leg is demoted and re-protected")
r3 = restarted()                       # live-ish: recovered with real resting stops
check("recovered stop is flagged AT BROKER", r3.position.ce.sl_at_broker, True)
check("recovered stop trigger", r3.position.ce.sl_trigger, 40)

# now the broker's book no longer contains it (cancelled by hand in Kite, say)
r3.executor._live = True                                  # force the live path
r3.executor.kite.orders = lambda: [o for o in FakeKite().orders() if o.get("order_type") != "SL"]
r3._last_stop_poll = 0
r3._detect_stops()
check("leg demoted when its stop is gone", r3.position.ce.sl_at_broker, False)
check("demoted leg counts as unprotected", len(r3.position.unprotected_legs()), 2)
check("the disappearance is logged", len(evs(r3, "stop_vanished")), 2)

# ══ 13. the 15-minute status heartbeat ═══════════════════════════════════
print("\n [13] 15-minute status heartbeat into the audit log")
import live.controller as _cmod
beats = []
_orig_audit = _cmod.audit_log
_cmod.audit_log = lambda idx, ev, **f: beats.append((ev, f))
try:
    c10 = new_ctrl(dte=0)
    step(c10, calm, "09:30:02")
    step(c10, calm, "09:31:00", times=5)          # same 15-min slot
    n_after_first = len([b for b in beats if b[0] == "STATUS"])
    step(c10, calm, "09:46:00")                    # next slot
    step(c10, calm, "10:02:00")                    # next slot
    status = [b for b in beats if b[0] == "STATUS"]
finally:
    _cmod.audit_log = _orig_audit

check("one heartbeat per 15-min slot, not per tick", n_after_first, 1)
check("a heartbeat per slot thereafter", len(status), 3)
last = status[-1][1]
check("heartbeat records the shape", last["shape"], "strangle")
check("heartbeat records both legs", last["legs"].count("|"), 1)
check("heartbeat shows the live premium", "now=" in last["legs"], True)
check("heartbeat shows distance to stop", "to_sl=" in last["legs"], True)
check("heartbeat states the SL is PAPER, not broker-confirmed",
      "SL_PAPER" in last["legs"], True)
check("heartbeat carries MTM", "mtm" in last, True)
check("heartbeat flags unprotected count", last["unprotected"], 0)
check("every action is audited, paper included",
      len([b for b in beats if b[0] == "LEG_OPEN"]), 2)

# ══ 14. expiry-day stop tightening ═══════════════════════════════════════
print("\n [14] 0-DTE stop schedule — 40 → 30 at 12:00, → 20 at 14:00")
c11 = new_ctrl(dte=0)
step(c11, calm, "09:30:02")
check("day opens on the 40 stop", c11.sl, 40)
check("both legs carry 40", (c11.position.ce.sl_trigger, c11.position.pe.sl_trigger), (40, 40))

# 11:59 — before the step, nothing moves even though both legs are cheap
step(c11, calm, "11:59:00")
check("no tightening before 12:00", c11.sl, 40)

# 12:00, but the CE has run to 34 — that is NOT 10 below the 40 stop, so the
# step is DUE but cannot arm. A buy-stop at 30 under a market of 34 is impossible.
hot12 = chain_of({1: 60, 2: 48, 3: 34, 4: 22}, {1: 26, 2: 18, 3: 12, 4: 8})
step(c11, hot12, "12:00:05")
check("step deferred while a leg is above the arm level", c11.sl, 40)
check("legs keep their 40 stop", c11.position.ce.sl_trigger, 40)
check("the deferral is logged", len(evs(c11, "sl_step_deferred")), 1)
check("nothing was tightened yet", len(evs(c11, "sl_tightened")), 0)

step(c11, hot12, "12:01:00", times=8)
check("deferral logged once, not once per tick", len(evs(c11, "sl_step_deferred")), 1)

# CE falls back to 27 — now BOTH legs are 10+ below the 40 stop, so it arms
cool12 = chain_of({1: 44, 2: 33, 3: 27, 4: 18}, {1: 24, 2: 17, 3: 11, 4: 7})
step(c11, cool12, "12:04:00")
check("step arms once both legs qualify", c11.sl, 30)
check("CE stop moved to 30", c11.position.ce.sl_trigger, 30)
check("PE stop moved to 30", c11.position.pe.sl_trigger, 30)
check("new limit follows the trigger (30 → 32)", c11.position.ce.sl_limit, 32.0)
check("both legs still protected", len(c11.position.unprotected_legs()), 0)
check("the tightening is logged", len(evs(c11, "sl_tightened")), 1)
check("history records the move", c11.sl_history[0]["from"], 40)

# a leg opened AFTER the step is born on the tightened stop
c11.position.retire(PE)                                   # simulate the PE leaving
step(c11, cool12, "12:15:05")
check("re-entered leg is born with the 30 stop", c11.position.pe.sl_trigger, 30)

# 14:00 — second step to 20, both legs must be at/below 20
step(c11, cool12, "14:00:05")
check("14:00 step deferred while legs sit above 20", c11.sl, 30)
late = chain_of({1: 18, 2: 13, 3: 9, 4: 6}, {1: 16, 2: 11, 3: 8, 4: 5})
step(c11, late, "14:02:00")
check("second step arms", c11.sl, 20)
check("CE stop now 20", c11.position.ce.sl_trigger, 20)
check("PE stop now 20", c11.position.pe.sl_trigger, 20)
check("two tightenings recorded", len(c11.sl_history), 2)
check("no further steps pending", c11.sl_step_i, 2)

# ── the schedule is EXPIRY DAY only ──────────────────────────────────────
c12 = new_ctrl(dte=1)
check("1-DTE has no schedule", c12.sl_steps, [])
check("1-DTE stop is the flat table value", c12.sl, 50)
step(c12, calm, "09:30:02")
step(c12, calm, "12:05:00")
check("1-DTE never tightens", c12.sl, 50)
check("1-DTE legs keep 50", c12.position.ce.sl_trigger, 50)

# ── Sensex 0-DTE: 80 -> 60 -> 40, steps of 20 not 10 ────────────────────
print("\n [14b] Sensex 0-DTE schedule — the arming rule must handle a 20-wide step")
c13 = new_ctrl(dte=0, index="SENSEX")
check("Sensex 0-DTE opens on 80", c13.sl, 80)
check("Sensex 0-DTE has two steps", [(a, v) for a, v in c13.sl_steps],
      [("12:00", 60.0), ("14:00", 40.0)])

SIV, SATM = 100, 77_400


def schain(ce, pe, atm=SATM):
    c = {(atm, CE): 200.0, (atm, PE): 200.0}
    for n, p in ce.items():
        c[(atm + n * SIV, CE)] = p
    for n, p in pe.items():
        c[(atm - n * SIV, PE)] = p
    return c


s_open = schain({1: 60, 2: 48, 3: 39, 4: 30}, {1: 58, 2: 46, 3: 38, 4: 29})
c13.on_tick(s_open, SATM, at("09:30:02"))
check("Sensex entered", c13.position.is_complete, True)
check("Sensex legs carry the 80 stop", c13.position.ce.sl_trigger, 80)
check("Sensex SL limit uses its own 4-pt buffer (80 -> 84)",
      c13.position.ce.sl_limit, 84.0)

# THE case that was broken: a leg at 65 is 10+ below the 80 stop, but a buy-stop
# at 60 cannot sit under a market of 65. The step must NOT arm.
hot_s = schain({1: 95, 2: 78, 3: 65, 4: 52}, {1: 40, 2: 30, 3: 22, 4: 16})
c13.on_tick(hot_s, SATM, at("12:00:05"))
check("step does NOT arm at premium 65 (below 80-10, but above the new 60)",
      c13.sl, 80)
check("deferral logged", len(evs(c13, "sl_step_deferred")), 1)

# once both legs are under 60 it arms
cool_s = schain({1: 80, 2: 66, 3: 55, 4: 42}, {1: 38, 2: 28, 3: 20, 4: 14})
c13.on_tick(cool_s, SATM, at("12:04:00"))
check("arms once both legs are below the NEW stop", c13.sl, 60)
check("Sensex CE stop moved to 60", c13.position.ce.sl_trigger, 60)
check("Sensex limit follows (60 -> 64)", c13.position.ce.sl_limit, 64.0)

# 14:00 -> 40, same rule again
c13.on_tick(cool_s, SATM, at("14:00:05"))
check("14:00 step deferred while a leg is above 40", c13.sl, 60)
late_s = schain({1: 34, 2: 26, 3: 19, 4: 13}, {1: 30, 2: 22, 3: 16, 4: 11})
c13.on_tick(late_s, SATM, at("14:03:00"))
check("second Sensex step arms", c13.sl, 40)
check("both Sensex tightenings recorded", len(c13.sl_history), 2)

# 1-DTE Sensex is untouched
c14b = new_ctrl(dte=1, index="SENSEX")
check("Sensex 1-DTE has no schedule", c14b.sl_steps, [])
check("Sensex 1-DTE stays flat at 80", c14b.sl, 80)

# ══ 15. both legs stopped out → done for the day ═════════════════════════
print("\n [15] both SLs hit → no re-entry, day over")
c14 = new_ctrl(dte=0)
step(c14, chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10}), "09:30:02")
check("started complete", c14.position.is_complete, True)

# CE runs through its 40 stop — routine, we go single-legged and wait for a window
ce_out = chain_of({1: 70, 2: 55, 3: 42, 4: 30, 5: 20, 6: 10}, {1: 22, 2: 16, 3: 12, 4: 8})
step(c14, ce_out, "10:05:00")
check("one stop → single leg, still trading", c14.position.is_single, True)
check("not done after ONE stop", c14.done, False)

# now the PE is stopped too, BEFORE any re-entry window → flat by stop-outs
both_out = chain_of({1: 70, 2: 55, 3: 42, 4: 30, 5: 20, 6: 10},
                    {1: 60, 2: 52, 3: 45, 4: 38})
step(c14, both_out, "10:08:00")
check("two stops → flat", c14.position.is_flat, True)
check("day is over", c14.done, True)
check("reason recorded", "both legs stopped out" in (c14.kill_reason or ""), True)
check("the decision is logged", len(evs(c14, "stopped_out_flat")), 1)
check("two stop-outs in history", len([l for l in c14.position.history
                                       if l.exit_reason == "stop loss"]), 2)

# and it must STAY over — no window may re-open anything
for w in ("10:15:05", "10:30:05", "11:00:05", "14:00:05"):
    step(c14, chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10}), w)
check("no fresh strangle at any later window", c14.position.is_flat, True)
check("no entry_complete after the stop-out", len(evs(c14, "entry_complete")), 1)
check("fresh-entry counter untouched", c14.fresh_entries, 1)

# a leg CLOSED by an adjustment (not stopped) must NOT end the day
c15 = new_ctrl(dte=0)
step(c15, chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10}), "09:30:02")
step(c15, chain_of({1: 60, 2: 45, 3: 30, 4: 20}, {1: 28, 2: 20, 3: 12, 4: 8}), "10:00:05")
check("an adjustment does not end the day", c15.done, False)
check("still holding a strangle", c15.position.is_complete, True)

# ══ 16. the skipped-stop watchdog ════════════════════════════════════════
print("\n [16] exchange stop skipped → watchdog covers 10 points past it")
c16 = new_ctrl(dte=0)
step(c16, chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10}), "09:30:02")
check("watchdog gap from config", c16.sl_watchdog_gap, 10.0)
# Simulate the LIVE hole: the exchange stop never fills. Paper would fill it at the
# trigger, so stop detection is disabled to reproduce the skipped case.
c16._detect_stops = lambda: None

# 47 = trigger 40 + 7 → inside the gap, watchdog must NOT fire yet
step(c16, chain_of({1: 90, 2: 70, 3: 47, 4: 33}, {1: 12, 2: 8, 3: 5, 4: 3}), "10:05:00")
check("no force-cover inside the gap", c16.position.is_complete, True)
check("nothing logged yet", len(evs(c16, "stop_skipped")), 0)

# 51 = trigger 40 + 11 → past the gap, watchdog fires
step(c16, chain_of({1: 95, 2: 75, 3: 51, 4: 36}, {1: 12, 2: 8, 3: 5, 4: 3}), "10:06:00")
check("watchdog force-covered the leg", len(evs(c16, "stop_skipped")), 1)
check("CE is gone", c16.position.ce, None)
check("single-legged now", c16.position.is_single, True)
gone = c16.position.history[-1]
check("booked AS a stop loss, not a normal exit",
      gone.exit_reason.startswith("stop loss"), True)
check("reason names the skipped trigger", "skipped at 40" in gone.exit_reason, True)
check("covered at the market price it had run to", gone.exit_price, 51.0)
check("the surviving PE is untouched", c16.position.pe.is_live, True)

# the watchdog follows the TIGHTENED stop, not the opening one
c17 = new_ctrl(dte=0)
step(c17, chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10}), "09:30:02")
cool = chain_of({1: 26, 2: 19, 3: 13, 4: 9}, {1: 24, 2: 17, 3: 11, 4: 7})
step(c17, cool, "12:01:00")
check("stop tightened to 30", c17.sl, 30)
c17._detect_stops = lambda: None
# 38 is only 8 past the NEW 30 stop... wait, it is 8 past 30 → inside the gap
step(c17, chain_of({1: 70, 2: 52, 3: 38, 4: 26}, {1: 10, 2: 7, 3: 5, 4: 3}), "12:10:00")
check("inside the gap of the TIGHTENED stop", len(evs(c17, "stop_skipped")), 0)
# 41 is 11 past 30 → fires, even though it is only 1 past the ORIGINAL 40
step(c17, chain_of({1: 74, 2: 56, 3: 41, 4: 28}, {1: 10, 2: 7, 3: 5, 4: 3}), "12:11:00")
check("watchdog uses the tightened stop, not the opening one",
      len(evs(c17, "stop_skipped")), 1)

# a watchdog cover that leaves us flat ends the day, like any other stop-out
c18 = new_ctrl(dte=0)
step(c18, chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10}), "09:30:02")
c18._detect_stops = lambda: None
step(c18, chain_of({1: 95, 2: 75, 3: 51, 4: 36}, {1: 95, 2: 75, 3: 51, 4: 36}), "10:06:00")
check("both legs force-covered", len(evs(c18, "stop_skipped")), 2)
check("flat", c18.position.is_flat, True)
check("day over, same as a normal double stop-out", c18.done, True)

# ── a cover that does NOT fill ────────────────────────────────────────────
# The exit path's worst case: max loss fires, the buy-backs are refused, and the
# stops have already been cancelled. This used to write both legs closed at an
# invented price and end the day — leaving a real naked short at the broker with
# nothing watching it. The book must now stay honest and keep trying until filled.
from live.executor import Fill as _Fill

c19 = new_ctrl(dte=0)
c19.max_loss = 1000.0
step(c19, chain_of({1: 30, 2: 22, 3: 16, 4: 11}, {1: 28, 2: 21, 3: 15, 4: 10}), "09:30:02")
check("entered", c19.position.is_complete, True)

# broker refuses every buy from here
c19.executor.buy = lambda leg, mark, kind="exit": _Fill(f"rej-{leg.strike}", None, None,
                                                        status="NOFILL")
# entry 16 + 15 = 31 combined; 1000/65 = 15.4 pts, so 55 combined breaches it while
# BOTH legs stay under the 40 stop — the cover path, not a stop-out
_blown = chain_of({1: 30, 2: 22, 3: 30, 4: 11}, {1: 28, 2: 21, 3: 25, 4: 10})
step(c19, _blown, "10:00:00")

check("max loss fired", c19.killed, True)
check("legs NOT faked closed", c19.position.n_live, 2)
check("book not reported flat", c19.position.is_flat, False)
check("both sides stuck", sorted(c19.stuck), sorted([CE, PE]))
check("day NOT done while short", c19.done, False)
check("cover_failed logged", len(evs(c19, "cover_failed")) >= 2, True)
check("flatten_failed logged", len(evs(c19, "flatten_failed")), 1)

_before = c19.stuck_attempts
for _i in range(5):
    c19._last_stuck_try = 0.0                 # bypass the 2s throttle in the test
    step(c19, _blown, f"10:0{_i + 1}:00")
check("kept retrying", c19.stuck_attempts > _before, True)
check("still short, still not done", c19.position.n_live == 2 and not c19.done, True)

# broker starts accepting again
c19.executor.buy = lambda leg, mark, kind="exit": _Fill(f"ok-{leg.strike}", mark, "10:07:00")
c19._last_stuck_try = 0.0
step(c19, _blown, "10:07:00")
check("finally flat", c19.position.is_flat, True)
check("stuck cleared", c19.stuck, {})
check("done only once genuinely out", c19.done, True)
check("cover_recovered logged", len(evs(c19, "cover_recovered")) >= 1, True)
check("flatten_complete logged", len(evs(c19, "flatten_complete")), 1)

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
