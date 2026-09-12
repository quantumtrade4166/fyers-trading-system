"""
core/selector.py — strike selection over a LISTED-strike grid.
==============================================================

Ported from the NSE delta-neutral strangle, with one structural change.

On NSE every strike is `interval` apart, so the original walked OTM levels
arithmetically: `atm + n * interval`. Delta's BTC chain is NOT uniform — today's
daily expiry lists 100 near the money, then 200, 500 and 1000 further out (85
strikes from 58,000 to 90,000). Stepping by a constant would skip real strikes
and invent ones that do not exist.

So the ladder is built from the strikes that are actually LISTED, sorted and
walked outward from ATM. This keeps the two properties the strategy depends on:

  - "OTM level 1" still means "the first strike outside the money", whatever the
    gap happens to be there
  - ATM is excluded by construction, so "never sell ATM" is a property of the
    data structure rather than a check someone can forget to call

Everything here is a pure function over a plain dict — no network, no clock, no
exchange — so every rule is exactly testable (`test_selector.py`).

Chain format:   {(strike: float, "CE"|"PE"): premium: float}

The three rules, unchanged from the NSE strategy:

ENTRY        scan OTM1 outward, take the premium CLOSEST to target; a configured
             preferred band wins over a closer-to-target strike outside it
RE-ENTRY     the HIGHEST premium STRICTLY BELOW the open leg's current premium,
             but above open/ratio, else the new leg is born already imbalanced
SL GATE      a re-entry must also sit below its own stop, or the stop kills it
             the instant it is placed
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

CE, PE = "CE", "PE"


def listed_strikes(chain: dict) -> list[float]:
    """Every strike present in the chain, ascending."""
    return sorted({k[0] for k in chain})


def atm_strike(spot: float, strikes) -> float | None:
    """The listed strike nearest to spot — the one strike we may NEVER sell.

    Takes the STRIKE LIST, not an interval: on a non-uniform grid there is no
    single interval to round to. A tie (spot exactly between two listed strikes)
    goes to the LOWER strike, deterministically, so two processes reading the same
    chain always agree on what ATM is.
    """
    strikes = sorted(strikes)
    if not strikes or spot is None:
        return None
    return min(strikes, key=lambda s: (abs(s - spot), s))


def otm_ladder(chain: dict, atm: float, opt_type: str,
               max_levels: int = 40) -> list[tuple[int, float, float]]:
    """[(otm_level, strike, premium), ...] from OTM1 outward.

    OTM is directional: calls get cheaper going UP from ATM, puts going DOWN. ATM
    itself is never included. Strikes with no premium quoted are skipped WITHOUT
    consuming a level — an unquoted strike is not a rung of the ladder, it is a
    hole in it, and counting it would make "OTM1" mean different things on the two
    sides of the same chain.
    """
    strikes = listed_strikes(chain)
    if atm is None:
        return []
    if opt_type == CE:
        cand = [s for s in strikes if s > atm]
    else:
        cand = [s for s in reversed(strikes) if s < atm]
    out = []
    for s in cand:
        prem = chain.get((s, opt_type))
        if prem is None:
            continue
        out.append((len(out) + 1, s, float(prem)))
        if len(out) >= max_levels:
            break
    return out


def _result(level, strike, prem, why, **extra) -> dict:
    return {"otm_level": level, "strike": strike, "premium": round(prem, 2),
            "why": why, **extra}


def select_entry_leg(chain: dict, atm: float, opt_type: str, *,
                     target: float, prefer_min: float = None,
                     prefer_max: float = None, max_levels: int = 40) -> dict | None:
    """The entry strike for one side. None if the chain has no usable strike.

    Tie-break (two strikes equally close to target): take the FURTHER OTM one —
    same credit either way, less gamma.
    """
    ladder = otm_ladder(chain, atm, opt_type, max_levels)
    if not ladder:
        return None

    # OTM1 already at/below target -> nothing further out can be closer to it,
    # because premiums only fall as you move away from the money.
    lvl1, strike1, prem1 = ladder[0]
    if prem1 <= target:
        return _result(lvl1, strike1, prem1, "otm1 at/below target — best available")

    banded = []
    if prefer_min is not None and prefer_max is not None:
        banded = [c for c in ladder if prefer_min <= c[2] <= prefer_max]

    pool, why = (banded, f"closest to {target} inside preferred {prefer_min}-{prefer_max}") \
        if banded else (ladder, f"closest to target {target}")
    lvl, strike, prem = min(pool, key=lambda c: (abs(c[2] - target), -c[0]))
    return _result(lvl, strike, prem, why)


def select_reentry_leg(chain: dict, atm: float, opt_type: str, *,
                       below_premium: float, ratio: float = 2.0, sl: float = None,
                       max_levels: int = 40) -> dict | None:
    """The re-entry strike: the FIRST strike priced below the leg that is still
    alive. Walk out from the money and take the highest premium under it.

    TWO bounds, not three:
      upper  premium <  below_premium   cheaper than the alive leg, so replacing the
                                        small leg cannot flip the imbalance instead
                                        of fixing it
      stop   premium <  sl              else the leg is born past its own stop and
                                        the broker kills it on placement

    WHAT WAS REMOVED, AND WHY IT MATTERED
    There used to be a third bound: `premium > below_premium / ratio`. The idea was
    that a replacement worth less than half the alive leg is ALREADY 2x-imbalanced,
    so the next window would just want to replace it again — better to wait.

    In practice it refused to re-enter 64 times in four days of live paper. It only
    ever bites when the alive leg has run a long way, which is exactly when the
    other side is deep out-of-the-money and cheap — precisely the moment you most
    want both legs back on. The strategy sat single-legged instead, and a
    single-legged short strangle is a directional bet nobody chose to make.

    The churn it was guarding against is handled where it belongs, in the caller:
    if the replacement lands on the strike just closed, the position is left alone.

    Returns None only when the side is genuinely empty or everything is above the
    stop.
    """
    ladder = otm_ladder(chain, atm, opt_type, max_levels)
    valid = [c for c in ladder if c[2] < below_premium]
    if not valid:
        return None
    valid.sort(key=lambda c: -c[2])            # highest premium first = first below

    if sl is None:
        lvl, strike, prem = valid[0]
        return _result(lvl, strike, prem, f"first below open leg {below_premium}")

    ok = [c for c in valid if c[2] < sl]
    if not ok:
        return None
    lvl, strike, prem = ok[0]
    if valid[0][2] >= sl:
        return _result(lvl, strike, prem,
                       f"first below open leg {below_premium}, stepped out past SL {sl}",
                       sl_gated=True, sl_gated_from=valid[0][1],
                       sl_gated_skipped=len(valid) - len(ok))
    return _result(lvl, strike, prem, f"first below open leg {below_premium}")


def needs_adjustment(ce_premium: float, pe_premium: float,
                     ratio: float = 2.0) -> tuple[bool, str | None]:
    """(triggered, side_to_replace). Fires when one leg's premium has reached
    `ratio`x the other's. The SMALLER leg is replaced — it has drifted too far
    from the money to be doing any work."""
    if ce_premium is None or pe_premium is None:
        return False, None
    if ce_premium <= 0 or pe_premium <= 0:
        return False, None
    if ce_premium >= ratio * pe_premium:
        return True, PE
    if pe_premium >= ratio * ce_premium:
        return True, CE
    return False, None
