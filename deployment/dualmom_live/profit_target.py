"""
profit_target.py - flatten the Kotak Rohit book once unrealised P&L reaches a target.

WHAT IT DOES
    Every tick it prices the live holdings and sums qty x (ltp - avg). When that
    reaches `target_rs` it exits the WHOLE book to cash, through the same
    rebalance.plan() / runner.execute() path the monthly job uses - so it
    inherits the resolve brake, the margin halt, the fill polling and the ledger
    capture instead of hand-rolling a sell loop.

    This is a profit target, not the -35% stop (that lives in runner/rebalance
    and is unchanged). The two are independent and may both be active.

OWNER ARMS IT, NOT THE CODE
    Disarmed by default. The control file is the only switch:

        deployment/dualmom_live_state/profit_target.json
        {"armed": false, "target_rs": 2000, "fired": null}

    Nothing fires while "armed" is false, and the file is never written to arm
    itself.

IT WILL NOT FIRE ON AN INCOMPLETE P&L
    If ANY held position has no live mark, the sum is not the book's P&L - it is
    the P&L of the part we could price. Firing on that could exit the whole book
    on a number that was never true. So a missing mark SKIPS the tick and says
    which symbol was missing. Same rule as cycle_report: an unknown is not zero.

    This matters more than it looks. On 2026-09-10 last_prices() returned a LIST
    where the caller expected a dict, every lookup raised, and the 15:25 stop
    check silently fell back to entry prices - a stop that could never trigger.
    A target that cannot see prices must go quiet and loud, never quiet and calm.

ONE SHOT
    Once a flatten completes, `fired` is stamped and the target will not arm
    itself again - otherwise the next tick would see a flat book, a P&L of zero,
    and nothing to do, but a PARTIAL exit could churn the remainder. Re-arming is
    an owner action: clear `fired`.

    A flatten that HALTED (the resolve brake, a margin rejection) is NOT stamped,
    because nothing was sold. It will be retried on the next tick, and every
    attempt is counted so a permanently broken exit is visible rather than silent.
"""

import json
from datetime import datetime
from pathlib import Path

from deployment.dualmom_live import book as B
from deployment.dualmom_live import kotak_equity as K
from deployment.dualmom_live import ledger as L
from deployment.dualmom_live import rebalance as R
from deployment.dualmom_live import runner as RUN

STATE = Path(__file__).resolve().parents[1] / "dualmom_live_state"
CONTROL = STATE / "profit_target.json"

DEFAULT = {"armed": False, "target_rs": 2000.0, "fired": None, "attempts": 0}


# -- control ------------------------------------------------------------------

def control() -> dict:
    """Read the owner's switch. Missing/corrupt file reads as DISARMED."""
    try:
        d = json.loads(CONTROL.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("not an object")
    except Exception:
        d = {}
    out = dict(DEFAULT)
    out.update({k: d[k] for k in DEFAULT if k in d})
    out["armed"] = bool(out["armed"])
    try:
        out["target_rs"] = float(out["target_rs"])
    except Exception:
        out["target_rs"] = DEFAULT["target_rs"]
    return out


def _write(d: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    cur = control()
    cur.update(d)
    cur["updated"] = datetime.now().isoformat(timespec="seconds")
    CONTROL.write_text(json.dumps(cur, indent=2), encoding="utf-8")


# -- the P&L ------------------------------------------------------------------

def positions(client) -> dict:
    """{symbol: {qty, avg_price}} for what we hold RIGHT NOW.

    broker_positions(), not holdings(): holdings() is pre-settlement, so it still
    shows a share sold this morning and shows nothing at all for a basket bought
    today (T+1). Both would mis-state the P&L this target fires on. Same source
    the -35% stop uses, so the two triggers can never disagree about the book.
    """
    return {s: {"qty": int(p["qty"]), "avg_price": float(p.get("avg_price") or 0.0)}
            for s, p in B.broker_positions(client).items() if int(p["qty"]) > 0}


def unrealised(client, holds: dict = None, marks: dict = None) -> dict:
    """Unrealised P&L across the live book.

    Returns {pnl, cost, market_value, positions, priced, missing, rows}.
    `missing` non-empty means `pnl` is NOT the book's P&L - do not act on it.
    """
    holds = positions(client) if holds is None else holds
    syms = sorted(holds)
    marks = K.last_prices(client, syms) if marks is None else marks

    rows, missing, pnl, cost, mv = [], [], 0.0, 0.0, 0.0
    for s in syms:
        qty = int(holds[s]["qty"])
        avg = float(holds[s].get("avg_price") or 0.0)
        ltp = marks.get(s)
        if not ltp or not avg:
            missing.append(s)
            continue
        c = qty * avg
        v = qty * float(ltp)
        cost += c
        mv += v
        pnl += v - c
        rows.append({"symbol": s, "qty": qty, "avg": avg, "ltp": float(ltp),
                     "pnl": round(v - c, 2),
                     "pnl_pct": round((v / c - 1) * 100, 3) if c else None})
    rows.sort(key=lambda r: -r["pnl"])
    return {"pnl": round(pnl, 2), "cost": round(cost, 2),
            "market_value": round(mv, 2), "positions": len(syms),
            "priced": len(rows), "missing": missing, "rows": rows}


# -- the flatten ---------------------------------------------------------------

def flatten(client, reason: str, marks: dict = None, holds: dict = None) -> dict:
    """Exit the whole book to cash via the normal plan/execute path."""
    holds = positions(client) if holds is None else holds
    current = {s: int(v["qty"]) for s, v in holds.items() if int(v["qty"]) > 0}
    if not current:
        return {"halted": "nothing held", "orders": []}

    syms = sorted(current)
    marks = K.last_prices(client, syms) if marks is None else marks
    nav = K.account_value(client, marks)["nav"]

    # A synthetic OUT signal is what makes rebalance.plan() emit a sell for
    # every held name. Reusing it (rather than building orders here) keeps ONE
    # code path to the broker, so the brakes cannot drift apart.
    sig = {"signal": "OUT", "date": datetime.now().strftime("%Y-%m-%d"),
           "holdings": []}
    run = {"started": datetime.now().isoformat(timespec="seconds"), "log": [],
           "trigger": "profit_target", "reason": reason}
    run["signal"] = sig
    run["plan"] = R.plan(sig, current, marks, nav)
    RUN.save(run, f"profit_target_plan_{datetime.now():%Y%m%d_%H%M%S}.json")

    run = RUN.execute(run, client)
    RUN.save(run, f"profit_target_run_{datetime.now():%Y%m%d_%H%M%S}.json")
    try:
        L.capture(client)
    except Exception as e:
        run.setdefault("log", []).append(f"ledger capture after flatten failed: {e}")
    return run


# -- the tick ------------------------------------------------------------------

def check(client) -> dict:
    """One evaluation. Safe to call on a timer; returns what it decided and why."""
    ctl = control()
    now = datetime.now().isoformat(timespec="seconds")
    base = {"at": now, "armed": ctl["armed"], "target_rs": ctl["target_rs"]}

    if not ctl["armed"]:
        return {**base, "action": "skip", "why": "not armed"}
    if ctl.get("fired"):
        return {**base, "action": "skip", "why": f"already fired at {ctl['fired']}"}

    holds = positions(client)
    if not holds:
        return {**base, "action": "skip", "why": "nothing held"}
    marks = K.last_prices(client, sorted(holds))
    u = unrealised(client, holds=holds, marks=marks)

    if u["missing"]:
        # Loud, not calm: a target that cannot price the book must say so.
        return {**base, "action": "skip",
                "why": f"no live mark for {len(u['missing'])} of {u['positions']} "
                       f"positions ({', '.join(u['missing'][:6])}) - P&L incomplete, "
                       f"refusing to act on a partial number",
                "pnl_partial": u["pnl"], "missing": u["missing"]}

    if u["pnl"] < ctl["target_rs"]:
        return {**base, "action": "hold", "why": "below target",
                "pnl": u["pnl"], "gap": round(ctl["target_rs"] - u["pnl"], 2),
                "positions": u["positions"]}

    reason = (f"unrealised P&L Rs {u['pnl']:,.2f} >= target Rs {ctl['target_rs']:,.2f} "
              f"on {u['positions']} positions")
    _write({"attempts": int(ctl.get("attempts") or 0) + 1})
    run = flatten(client, reason, marks=marks, holds=holds)

    orders = run.get("orders") or []
    filled = [o for o in orders if str(o.get("status")) == "FILLED"]
    done = bool(orders) and not run.get("halted") and len(filled) == len(orders)
    if done:
        _write({"fired": now, "fired_pnl": u["pnl"], "fired_reason": reason})

    return {**base, "action": "flatten", "why": reason, "pnl": u["pnl"],
            "orders": len(orders), "filled": len(filled),
            "halted": run.get("halted"), "complete": done,
            "log": run.get("log", [])}
