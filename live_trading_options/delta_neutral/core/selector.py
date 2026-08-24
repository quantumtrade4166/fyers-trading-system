"""
core/selector.py — strike selection rules.
==========================================

Pure functions over a plain chain dict — no network, no broker, no clock — so
every rule below can be tested exactly (`test_selector.py`). The controller does
the I/O and calls these to decide WHICH strike to sell.

Chain format used throughout:   {(strike:int, "CE"|"PE"): premium:float}

The two selection rules, verbatim from the strategy spec:

ENTRY (09:30)
  - scan OTM1 outward, never ATM
  - take the strike whose premium is CLOSEST to the target
  - if a preferred band is configured (Nifty 0-DTE: 15-20), a strike inside the
    band wins over a closer-to-target strike outside it
  - if even OTM1 is far below target (e.g. OTM1 = 10 vs target 20), OTM1 is the
    best available and is sold directly — premiums only fall as you go further
    out, so there is nothing better further along

RE-ENTRY (adjustment window, and after a leg is stopped out)
  - scan OTM1 outward, never ATM
  - take the HIGHEST premium that is STRICTLY BELOW the open leg's CURRENT
    premium ("just below" — 20.6 is rejected against an open leg at 20.30;
    19 is taken)
  - if nothing qualifies, return None and the caller SKIPS the whole window

SL GATE (re-entry only)
  A short leg's stop is a BUY trigger ABOVE its entry price. With a FIXED stop
  (40 for Nifty 0-DTE) a re-entry priced at, say, 44 would be born already past
  its own stop and be killed the instant the stop is placed. So a re-entry
  candidate must also sit below the stop level; if the "just below" strike fails
  that, we step further out to the first one that passes. The step is reported in
  the result (`sl_gated`) so it shows up in the log rather than happening silently.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

CE, PE = "CE", "PE"


def atm_strike(spot: float, interval: int) -> int:
    """Nearest strike to spot — the one strike we may NEVER sell."""
    return int(round(spot / interval) * interval)


def otm_ladder(chain: dict, atm: int, interval: int, opt_type: str,
               max_levels: int = 40) -> list[tuple[int, int, float]]:
    """[(otm_level, strike, premium), ...] from OTM1 outward.

    OTM is directional: calls get cheaper going UP from ATM, puts going DOWN.
    ATM itself is never included, so "never sell ATM" is structural here rather
    than a check someone can forget to call.
    """
    out = []
    for n in range(1, max_levels + 1):
        strike = atm + n * interval if opt_type == CE else atm - n * interval
        prem = chain.get((strike, opt_type))
        if prem is not None:
            out.append((n, strike, float(prem)))
    return out


def _result(level, strike, prem, why, **extra) -> dict:
    return {"otm_level": level, "strike": strike, "premium": round(prem, 2),
            "why": why, **extra}


def select_entry_leg(chain: dict, atm: int, interval: int, opt_type: str, *,
                     target: float, prefer_min: float = None,
                     prefer_max: float = None, max_levels: int = 40) -> dict | None:
    """The 09:30 entry strike for one side. None if the chain has no usable strike.

    Tie-break (two strikes equally close to target): take the FURTHER OTM one.
    Same credit either way, less gamma — the conservative side of a coin flip.
    """
    ladder = otm_ladder(chain, atm, interval, opt_type, max_levels)
    if not ladder:
        return None

    # OTM1 already below target -> nothing further out can be closer to it.
    lvl1, strike1, prem1 = ladder[0]
    if prem1 <= target:
        return _result(lvl1, strike1, prem1, "otm1 at/below target — best available")

    banded = []
    if prefer_min is not None and prefer_max is not None:
        banded = [c for c in ladder if prefer_min <= c[2] <= prefer_max]

    pool, why = (banded, f"closest to {target} inside preferred {prefer_min}-{prefer_max}") \
        if banded else (ladder, f"closest to target {target}")
    # key: distance to target first, then FURTHER OTM (higher level) on a tie
    lvl, strike, prem = min(pool, key=lambda c: (abs(c[2] - target), -c[0]))
    return _result(lvl, strike, prem, why)


def select_reentry_leg(chain: dict, atm: int, interval: int, opt_type: str, *,
                       below_premium: float, ratio: float = 2.0, sl: float = None,
                       max_levels: int = 40) -> dict | None:
    """The re-entry strike for one side: the HIGHEST premium that is strictly
    below `below_premium` (the open leg's current premium) while still being a
    genuine match for it.

    Three bounds, all of which must hold:
      upper  premium <  below_premium      "just below", never equal, never above
      lower  premium >  below_premium/ratio  else the new leg is ALREADY 2x-imbalanced
                                             against the open leg and the next window
                                             would immediately want to replace it
      stop   premium <  sl                   else it is born past its own stop

    The lower bound is what makes the spec's own example a skip: open CE = 21 with
    the best available PE at 9 is "below" but 9 <= 21/2, so re-selling it would
    just re-create the imbalance — hence "skip this window, wait for the next".

    Returns None when nothing qualifies, which the caller MUST treat as "skip the
    whole window" — never as "sell something close enough".
    """
    ladder = otm_ladder(chain, atm, interval, opt_type, max_levels)
    floor = below_premium / ratio if ratio else 0.0
    # strictly below the open leg, and not already imbalanced against it
    below = [c for c in ladder if c[2] < below_premium]
    valid = [c for c in below if c[2] > floor]
    if not valid:
        return None
    valid.sort(key=lambda c: -c[2])              # highest premium first = "just below"

    if sl is None:
        lvl, strike, prem = valid[0]
        return _result(lvl, strike, prem, f"just below open leg {below_premium}")

    # stop gate: a candidate at/above its own stop would be killed on placement
    ok = [c for c in valid if c[2] < sl]
    if not ok:
        return None
    lvl, strike, prem = ok[0]
    if valid[0][2] >= sl:
        return _result(lvl, strike, prem,
                       f"just below open leg {below_premium}, stepped out past SL {sl}",
                       sl_gated=True, sl_gated_from=valid[0][1],
                       sl_gated_skipped=len(valid) - len(ok))
    return _result(lvl, strike, prem, f"just below open leg {below_premium}")


def needs_adjustment(ce_premium: float, pe_premium: float,
                     ratio: float = 2.0) -> tuple[bool, str | None]:
    """(triggered, side_to_replace). Fires when one leg's premium has reached
    `ratio`x the other's — the premium-symmetry proxy for a delta imbalance.
    The SMALLER leg is the one replaced (it has drifted too far from the money).
    """
    if ce_premium is None or pe_premium is None:
        return False, None
    if ce_premium <= 0 or pe_premium <= 0:
        return False, None
    if ce_premium >= ratio * pe_premium:
        return True, PE                       # PE is the small one → replace PE
    if pe_premium >= ratio * ce_premium:
        return True, CE
    return False, None
