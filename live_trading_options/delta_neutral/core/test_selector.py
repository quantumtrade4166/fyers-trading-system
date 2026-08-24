"""
core/test_selector.py — strike-selection rule tests.
====================================================

Every worked example from the strategy spec is pinned here, plus the edge cases
that decide real money: never-ATM, strictly-below, the skip condition, and the
stop-loss gate on re-entries.

Run:  python live_trading_options/delta_neutral/core/test_selector.py
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.selector import (atm_strike, otm_ladder, select_entry_leg,
                           select_reentry_leg, needs_adjustment, CE, PE)

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def chain_from(atm, interval, ce_prems, pe_prems):
    """ce_prems[i] is the premium at OTM level i+1 (i.e. atm + (i+1)*interval)."""
    c = {(atm, CE): 100.0, (atm, PE): 100.0}      # ATM present but must never be picked
    for i, p in enumerate(ce_prems):
        c[(atm + (i + 1) * interval, CE)] = p
    for i, p in enumerate(pe_prems):
        c[(atm - (i + 1) * interval, PE)] = p
    return c


# ── ATM maths ─────────────────────────────────────────────────────────────
check("nifty atm rounds to 50", atm_strike(24_237, 50), 24_250)
check("nifty atm rounds down", atm_strike(24_220, 50), 24_200)
check("sensex atm rounds to 100", atm_strike(80_460, 100), 80_500)

# ── the ladder never contains ATM ─────────────────────────────────────────
ch = chain_from(24_250, 50, [30, 22, 16, 11, 7], [28, 21, 15, 10, 6])
lad = otm_ladder(ch, 24_250, 50, CE)
check("CE ladder starts at OTM1", lad[0][:2], (1, 24_300))
check("CE ladder excludes ATM", any(s == 24_250 for _, s, _ in lad), False)
check("PE ladder goes downward", otm_ladder(ch, 24_250, 50, PE)[0][1], 24_200)

# ── ENTRY: closest to target ──────────────────────────────────────────────
# Nifty 1-DTE, target 25 → CE ladder 30/22/16/11/7 → 22 is closest to 25
e = select_entry_leg(ch, 24_250, 50, CE, target=25)
check("entry 1-DTE picks premium closest to 25", e["premium"], 22)
check("entry never returns ATM", e["strike"] != 24_250, True)

# Nifty 0-DTE, target 20 with preferred band 15-20 → 16 is in-band, 22 is closer
# to 20 but outside the band. The band wins.
e0 = select_entry_leg(ch, 24_250, 50, CE, target=20, prefer_min=15, prefer_max=20)
check("entry 0-DTE prefers the 15-20 band over a closer out-of-band strike",
      e0["premium"], 16)
check("entry 0-DTE band reason recorded", "preferred" in e0["why"], True)

# spec case: OTM1 itself is far below target → sell OTM1 directly
cheap = chain_from(24_250, 50, [10, 7, 5], [9, 6, 4])
e1 = select_entry_leg(cheap, 24_250, 50, CE, target=20, prefer_min=15, prefer_max=20)
check("OTM1 below target → sell OTM1 directly", (e1["otm_level"], e1["premium"]), (1, 10))
check("OTM1-below-target reason recorded", "otm1" in e1["why"], True)

# tie-break: 18 and 22 are both 2 away from 20 → take the further OTM (18)
tie = chain_from(24_250, 50, [22, 18, 12], [22, 18, 12])
check("tie on distance → further OTM wins", select_entry_leg(tie, 24_250, 50, CE, target=20)["premium"], 18)

# ── RE-ENTRY: the spec's own worked example ───────────────────────────────
# open leg at 20.30; available 20.6 and 19  ->  19 (20.6 is ABOVE, rejected)
spec = {(24_250, CE): 99.0, (24_300, CE): 20.6, (24_350, CE): 19.0, (24_400, CE): 12.0}
r = select_reentry_leg(spec, 24_250, 50, CE, below_premium=20.30)
check("re-entry rejects 20.6 (above open leg 20.30)", r["premium"], 19.0)
check("re-entry picks strike 24350", r["strike"], 24_350)

# equal premium is NOT "just below"
eq = {(24_300, CE): 20.30, (24_350, CE): 17.0}
check("equal premium rejected → takes the lower one",
      select_reentry_leg(eq, 24_250, 50, CE, below_premium=20.30)["premium"], 17.0)

# highest-below wins (not the first one found)
many = {(24_300, CE): 19.9, (24_350, CE): 15.0, (24_400, CE): 11.0}
check("re-entry takes the HIGHEST premium below the open leg",
      select_reentry_leg(many, 24_250, 50, CE, below_premium=20.0)["premium"], 19.9)

# ── RE-ENTRY: the spec's skip example ─────────────────────────────────────
# open CE = 21, best PE available is 9. 9 < 21 but 9 <= 21/2, so re-selling it
# would immediately re-trigger the 2x rule → skip the window.
skip = {(24_250, PE): 45.0, (24_200, PE): 9.0, (24_150, PE): 6.0, (24_100, PE): 4.0}
check("CE=21 / best PE=9 → skip the window",
      select_reentry_leg(skip, 24_250, 50, PE, below_premium=21.0), None)

# same chain but the open leg is smaller (15) → PE 9 is now a genuine match
check("CE=15 / PE=9 → 9 is valid (above 15/2)",
      select_reentry_leg(skip, 24_250, 50, PE, below_premium=15.0)["premium"], 9.0)

# nothing at all below → skip
check("no strike below open leg → skip",
      select_reentry_leg({(24_300, CE): 50.0}, 24_250, 50, CE, below_premium=20.0), None)

# ── RE-ENTRY: the fixed-SL gate ───────────────────────────────────────────
# open leg at 60, SL fixed at 40. "Just below" is 58 — that would be born past
# its own stop, so step out to the first candidate under 40 (38).
gate = {(24_300, CE): 58.0, (24_350, CE): 38.0, (24_400, CE): 25.0}
g = select_reentry_leg(gate, 24_250, 50, CE, below_premium=60.0, sl=40)
check("SL gate steps past a candidate above the stop", g["premium"], 38.0)
check("SL gate is reported, not silent", g.get("sl_gated"), True)
check("SL gate records what it stepped from", g.get("sl_gated_from"), 24_300)

# without the gate the same chain would have taken 58
check("without SL the same chain takes 58",
      select_reentry_leg(gate, 24_250, 50, CE, below_premium=60.0)["premium"], 58.0)

# open leg so high that the floor (open/2) is above the SL → impossible → skip
check("open leg 100 with SL 40 → no valid re-entry, skip",
      select_reentry_leg(gate, 24_250, 50, CE, below_premium=100.0, sl=40), None)

# ── the 2x adjustment trigger ─────────────────────────────────────────────
check("21 vs 9 triggers, replace PE", needs_adjustment(21, 9), (True, PE))
check("9 vs 21 triggers, replace CE", needs_adjustment(9, 21), (True, CE))
check("exactly 2x triggers (>=)", needs_adjustment(20, 10), (True, PE))
check("just under 2x does not trigger", needs_adjustment(19.9, 10), (False, None))
check("balanced legs do not trigger", needs_adjustment(15, 14), (False, None))
check("a missing leg never triggers", needs_adjustment(20, None), (False, None))
check("a zero premium never triggers", needs_adjustment(20, 0), (False, None))

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
