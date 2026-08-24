"""
tools/make_demo_state.py — write a realistic day's state, for UI work only.
===========================================================================

Runs the REAL controller in paper mode through a scripted day and persists the
same three files the live engine writes, so the terminal can be built and checked
without waiting for market hours.

Everything it writes is prefixed with today's date and lives in
`data/live_state/`; delete those files (or just let the engine overwrite them on
the next trading day) when you are done.

    python live_trading_options/delta_neutral/tools/make_demo_state.py
    python live_trading_options/delta_neutral/tools/make_demo_state.py --clean
"""

import sys
import json
import argparse
import datetime as dt
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from live.controller import DNController, STATE_DIR
from core.selector import CE, PE

PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text())
IV, ATM = 50, 24_250
TODAY = dt.date.today()
DATE = TODAY.isoformat()


def chain_of(ce, pe, atm=ATM):
    c = {(atm, CE): 62.0, (atm, PE): 60.0}
    for n, p in ce.items():
        c[(atm + n * IV, CE)] = p
    for n, p in pe.items():
        c[(atm - n * IV, PE)] = p
    return c


def at(hms):
    h, m, s = (list(map(int, hms.split(":"))) + [0])[:3]
    return dt.datetime.combine(TODAY, dt.time(h, m, s))


def full_chain(spot, decay=1.0):
    """A plausible 0-DTE Nifty chain priced off the CURRENT spot.

    Premium = intrinsic + time value; time value falls with distance from spot AND
    with `decay`, which stands in for theta as expiry day runs on. Both matter:
    distance is what drives the 2x rule and the stops, and decay is what eventually
    brings both legs low enough for the 12:00 and 14:00 stop steps to arm.
    """
    c = {}
    for s in range(ATM - 700, ATM + 750, IV):
        d = abs(s - spot)
        tv = 55 * decay * (0.72 ** (d / IV))
        c[(s, CE)] = round(max(0.5, max(0.0, spot - s) + tv), 2)
        c[(s, PE)] = round(max(0.5, max(0.0, s - spot) + tv), 2)
    return c


def clean():
    n = 0
    for f in STATE_DIR.glob(f"{DATE}_*"):
        f.unlink()
        n += 1
    print(f"  removed {n} demo state file(s) for {DATE}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="delete today's state files and exit")
    a = ap.parse_args()
    if a.clean:
        return clean()

    ctrl = DNController("NIFTY", DATE, TODAY, 0, params=PARAMS,
                        lot_size=PARAMS["lot_sizes"]["NIFTY"], kite=None,
                        symbol_lookup=lambda s, t: f"NSE:NIFTY{s}{t}")

    # A scripted expiry day, in (time, spot, theta-decay) triples: a quiet open, a
    # grind up that trips the 2x rule, a squeeze that takes out the CE stop, then a
    # calm decaying afternoon so both stop steps (12:00 → 30, 14:00 → 20) can arm.
    script = [("09:30:02", 24250, 1.00), ("09:45:10", 24260, 0.97),
              ("10:00:05", 24300, 0.93), ("10:15:05", 24345, 0.88),
              ("10:30:05", 24380, 0.84), ("10:45:05", 24420, 0.80),
              ("11:00:05", 24470, 0.76), ("11:12:00", 24610, 0.72),
              ("11:15:05", 24605, 0.71), ("11:30:05", 24560, 0.66),
              ("11:45:05", 24540, 0.60), ("12:00:05", 24530, 0.54),
              ("12:10:00", 24520, 0.50), ("12:30:05", 24515, 0.44),
              ("13:00:05", 24510, 0.36), ("13:30:05", 24505, 0.28),
              ("14:00:05", 24500, 0.22), ("14:10:00", 24498, 0.18),
              ("14:30:05", 24495, 0.13), ("14:45:05", 24492, 0.09)]
    spot, decay = script[-1][1], script[-1][2]
    for when, s, dk in script:
        ctrl.on_tick(full_chain(s, dk), s, at(when))
    ctrl.persist()

    ch = full_chain(spot, decay)
    rows = []
    for k in range(ctrl.atm - 12 * IV, ctrl.atm + 13 * IV, IV):
        rows.append({"strike": k, "ce": ch.get((k, CE)), "pe": ch.get((k, PE)),
                     "is_atm": k == ctrl.atm})
    sold = {}
    for leg in (ctrl.position.ce, ctrl.position.pe):
        if leg is not None and leg.is_live:
            sold[f"{leg.strike}{leg.opt_type}"] = {"entry": leg.entry_price,
                                                   "sl": leg.sl_trigger,
                                                   "status": leg.status, "qty": leg.qty}
    now = dt.datetime.now().strftime("%H:%M:%S")
    # the real engine derives both files from ONE LiveChain, so keep spot/ATM
    # consistent here too or the demo shows two different ATMs
    demo_spot, demo_atm = spot, ctrl.atm
    (STATE_DIR / f"{DATE}_NIFTY_CHAIN.json").write_text(json.dumps({
        "index": "NIFTY", "spot": demo_spot, "atm": demo_atm, "rows": rows, "sold": sold,
        "expiry": DATE, "updated": now, "written": now}, indent=2))
    (STATE_DIR / f"{DATE}_NIFTY_TICK.json").write_text(json.dumps({
        "t": "14:45:05", "spot": demo_spot, "atm": demo_atm,
        "mtm": ctrl.position.mtm(ctrl.marks()), "realized": ctrl.position.realized(),
        "ce": ctrl._mark(ctrl.position.ce), "pe": ctrl._mark(ctrl.position.pe),
        "n_live": ctrl.position.n_live, "single": ctrl.position.is_single,
        "armed": False, "next_window_secs": 143, "updated": now}, indent=2))

    p = ctrl.position
    print(f"  wrote demo state for {DATE}")
    print(f"    position : {'strangle' if p.is_complete else 'single leg' if p.is_single else 'flat'}"
          f"  CE={p.ce.strike if p.ce else '—'} PE={p.pe.strike if p.pe else '—'}")
    print(f"    events   : {len(ctrl.events)}   history legs: {len(p.history)}")
    print(f"    MTM      : {p.mtm(ctrl.marks())}")
    print(f"    files    : {DATE}_NIFTY_DN.json / _CHAIN.json / _TICK.json")
    print("\n  clean up with:  python live_trading_options/delta_neutral/tools/make_demo_state.py --clean")


if __name__ == "__main__":
    main()
