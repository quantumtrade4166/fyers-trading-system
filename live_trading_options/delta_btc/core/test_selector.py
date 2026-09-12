"""
core/test_selector.py — strike-selection rule tests, BTC edition.
=================================================================

Every rule the NSE strategy pins, re-pinned here against a NON-UNIFORM strike
grid — which is the whole reason the selector was rewritten. On top of the
inherited cases there are new ones for the two things the uniform grid could
never express:

  - an OTM ladder whose gaps CHANGE as you walk outward (100, then 200, then 500)
  - a chain with HOLES in it (an unquoted strike must not consume an OTM level)

Run:  .venv/Scripts/python.exe live_trading_options/delta_btc/core/test_selector.py
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.selector import (atm_strike, otm_ladder, listed_strikes,
                           select_entry_leg, select_reentry_leg,
                           needs_adjustment, CE, PE)

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def chain_at(atm, ce_pairs, pe_pairs):
    """Build a chain from explicit (strike, premium) pairs, so a test can lay out
    an irregular grid exactly as the exchange lists it."""
    c = {(atm, CE): 900.0, (atm, PE): 900.0}   # ATM present but must never be picked
    for s, p in ce_pairs:
        c[(s, CE)] = p
    for s, p in pe_pairs:
        c[(s, PE)] = p
    return c


# a real-shaped BTC daily grid: 100 wide near ATM, 200 further out, then 500
GRID_CE = [(80_900, 220.0), (81_000, 190.0), (81_200, 148.0), (81_400, 108.0),
           (81_600, 78.0), (82_000, 39.0), (82_500, 18.0), (83_000, 9.0)]
GRID_PE = [(80_700, 210.0), (80_600, 180.0), (80_400, 140.0), (80_200, 95.0),
           (80_000, 55.0), (79_600, 19.0), (79_000, 8.0)]
CH = chain_at(80_800, GRID_CE, GRID_PE)

# ── ATM on a listed grid ──────────────────────────────────────────────────
STRIKES = listed_strikes(CH)
check("atm is the nearest LISTED strike", atm_strike(80_781, STRIKES), 80_800)
check("atm picks the nearest neighbour when spot sits between strikes",
      atm_strike(80_940, STRIKES), 80_900)
check("atm tie breaks to the LOWER strike deterministically",
      atm_strike(80_850, [80_800, 80_900]), 80_800)
check("atm with no strikes is None", atm_strike(80_800, []), None)
check("atm with no spot is None", atm_strike(None, STRIKES), None)

# ── the ladder walks LISTED strikes, gaps and all ─────────────────────────
lad = otm_ladder(CH, 80_800, CE)
check("CE ladder starts at OTM1", lad[0][:2], (1, 80_900))
check("CE ladder excludes ATM", any(s == 80_800 for _, s, _ in lad), False)
check("CE ladder follows the widening grid",
      [s for _, s, _ in lad][:6], [80_900, 81_000, 81_200, 81_400, 81_600, 82_000])
check("PE ladder walks DOWNWARD", [s for _, s, _ in otm_ladder(CH, 80_800, PE)][:4],
      [80_700, 80_600, 80_400, 80_200])
check("otm levels are consecutive despite uneven gaps",
      [n for n, _, _ in lad], list(range(1, len(lad) + 1)))

# a HOLE in the chain must not consume a level
holed = chain_at(80_800, [(80_900, 220.0), (81_000, None), (81_200, 148.0)], [])
holed = {k: v for k, v in holed.items() if v is not None}
hl = otm_ladder(holed, 80_800, CE)
check("an unquoted strike is skipped, not counted as a level",
      [(n, s) for n, s, _ in hl], [(1, 80_900), (2, 81_200)])

# ── ENTRY: closest to target ──────────────────────────────────────────────
e = select_entry_leg(CH, 80_800, CE, target=150)
check("entry picks the premium closest to target 150", e["premium"], 148.0)
check("entry never returns ATM", e["strike"] != 80_800, True)

# a preferred band beats a closer-to-target strike outside it
eb = select_entry_leg(CH, 80_800, CE, target=100, prefer_min=30, prefer_max=80)
check("preferred band wins over the closer out-of-band strike", eb["premium"], 78.0)
check("band reason recorded", "preferred" in eb["why"], True)

# OTM1 already below target -> sell OTM1 directly
cheap = chain_at(80_800, [(80_900, 40.0), (81_000, 25.0)], [])
e1 = select_entry_leg(cheap, 80_800, CE, target=150)
check("OTM1 below target → sell OTM1 directly", (e1["otm_level"], e1["premium"]), (1, 40.0))
check("OTM1-below-target reason recorded", "otm1" in e1["why"], True)

# tie on distance -> further OTM wins
tie = chain_at(80_800, [(80_900, 120.0), (81_000, 80.0), (81_200, 40.0)], [])
check("tie on distance → further OTM wins",
      select_entry_leg(tie, 80_800, CE, target=100)["premium"], 80.0)
check("entry on an empty side is None", select_entry_leg({}, 80_800, CE, target=100), None)

# ── RE-ENTRY: strictly below, and a genuine match ─────────────────────────
r = select_reentry_leg(CH, 80_800, CE, below_premium=200.0)
check("re-entry takes the HIGHEST premium strictly below the open leg",
      r["premium"], 190.0)
check("re-entry rejects the strike ABOVE the open leg", r["strike"], 81_000)
check("an empty side still returns None",
      select_reentry_leg({}, 80_800, CE, below_premium=200.0), None)

eq = chain_at(80_800, [(80_900, 190.0), (81_000, 148.0)], [])
check("equal premium is not 'just below' → takes the next one down",
      select_reentry_leg(eq, 80_800, CE, below_premium=190.0)["premium"], 148.0)

# THE RULE THAT CHANGED. There used to be a lower bound rejecting anything below
# open/ratio, which refused to re-enter 64 times in four days of live paper and
# left the book single-legged — a directional bet nobody chose. Now the first
# strike below the alive leg is taken, however cheap.
check("cheap side is taken, NOT skipped (this used to return None)",
      select_reentry_leg(chain_at(80_800, [(80_900, 90.0), (81_000, 40.0)], []),
                         80_800, CE, below_premium=210.0)["premium"], 90.0)
check("very cheap side is still taken",
      select_reentry_leg(chain_at(80_800, [(80_900, 9.0), (81_000, 4.0)], []),
                         80_800, CE, below_premium=210.0)["premium"], 9.0)
check("same chain, smaller open leg → 90 is still the pick",
      select_reentry_leg(chain_at(80_800, [(80_900, 90.0), (81_000, 40.0)], []),
                         80_800, CE, below_premium=150.0)["premium"], 90.0)
check("nothing below the open leg at all → skip",
      select_reentry_leg(chain_at(80_800, [(80_900, 500.0)], []),
                         80_800, CE, below_premium=200.0), None)

# ── RE-ENTRY: the SL gate ─────────────────────────────────────────────────
gate = chain_at(80_800, [(80_900, 290.0), (81_000, 190.0), (81_200, 120.0)], [])
g = select_reentry_leg(gate, 80_800, CE, below_premium=300.0, sl=200)
check("SL gate steps past a candidate at/above the stop", g["premium"], 190.0)
check("SL gate is reported, not silent", g.get("sl_gated"), True)
check("SL gate records what it stepped from", g.get("sl_gated_from"), 80_900)
check("without an SL the same chain takes 290",
      select_reentry_leg(gate, 80_800, CE, below_premium=300.0)["premium"], 290.0)
check("everything is above the stop → nothing placeable → skip",
      select_reentry_leg(chain_at(80_800, [(80_900, 500.0), (81_000, 400.0)], []),
                         80_800, CE, below_premium=600.0, sl=200), None)

# ── the 2x adjustment trigger (unchanged from NSE) ────────────────────────
check("210 vs 90 triggers, replace PE", needs_adjustment(210, 90), (True, PE))
check("90 vs 210 triggers, replace CE", needs_adjustment(90, 210), (True, CE))
check("exactly 2x triggers (>=)", needs_adjustment(200, 100), (True, PE))
check("just under 2x does not trigger", needs_adjustment(199, 100), (False, None))
check("balanced legs do not trigger", needs_adjustment(150, 140), (False, None))
check("a missing leg never triggers", needs_adjustment(200, None), (False, None))
check("a zero premium never triggers", needs_adjustment(200, 0), (False, None))

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
