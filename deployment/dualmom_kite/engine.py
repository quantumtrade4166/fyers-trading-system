"""
DualMom on Kite - plan, execute, monthly gate, stop check.

Strategy maths is NOT here: signal_engine.compute(), rebalance.plan() and
month_gate.decide() are the exact functions the Kotak account runs, so both
accounts hold the same basket. This module only supplies Kite's version of
"what do we hold, what is it worth, send the orders".
"""

import json
from datetime import datetime
from pathlib import Path

import pytz

from deployment.dualmom_kite import config as C
from deployment.dualmom_kite import kite_equity as K
from deployment.dualmom_kite import ledger as L
from deployment.dualmom_live import month_gate as G
from deployment.dualmom_live import rebalance as R
from deployment.dualmom_live import signal_engine as S

IST = pytz.timezone("Asia/Kolkata")
STATE = L.STATE


def _log(run: dict, msg: str):
    run["log"].append(f"{datetime.now(IST):%H:%M:%S} {msg}")
    print(f"  [dualmom-kite] {msg}", flush=True)


def month_signal_date(today=None):
    """Signal date governing THIS month: the last session before the 1st."""
    today = today or datetime.now(IST).date()
    first = today.replace(day=1)
    dates = [x.date() for x in S.load_prices().index if x.date() < first]
    return max(dates) if dates else None


def own_state(kite):
    """(current {symbol: qty}, nav, cash) from OUR book, priced live."""
    pos = L.own_book()["positions"]
    current = {s: int(p["qty"]) for s, p in pos.items()}
    marks = K.last_prices(kite, list(current)) if current else {}
    mv = sum(q * float(marks.get(s, pos[s]["avg_price"])) for s, q in current.items())
    cash = L.own_cash()["cash"]
    return current, mv + cash, cash, marks


def build_plan(kite, capital: float = None, as_of=None) -> dict:
    """Read-only. capital=None -> size against DualMom's own NAV on this account."""
    run = {"started": datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds"),
           "log": [], "account": C.CLIENT_ACCOUNT}
    as_of = as_of or month_signal_date()
    sig = S.compute(as_of=as_of)
    run["month_signal_date"] = str(as_of)
    _log(run, f"signal {sig['signal']} on {sig['date']} (nifty {sig['nifty_close']:,.2f} vs "
              f"MA {sig['nifty_ma']:,.2f}), universe {sig['universe_valid']}")
    current, nav, cash, held_marks = own_state(kite)
    if capital is not None:
        if current:
            raise ValueError("DualMom already holds positions on Kite - capital is fixed "
                             "by the book now; leave capital blank")
        nav = float(capital)
    syms = [h["symbol"] for h in sig.get("holdings", [])]
    live = K.last_prices(kite, syms + list(current))
    marks = {h["symbol"]: live.get(h["symbol"], h["price"]) for h in sig.get("holdings", [])}
    marks.update({s: live.get(s, held_marks.get(s, 0)) for s in current})
    unpriced = [s for s in syms if s not in live]
    if unpriced:
        run.setdefault("warnings", []).append(
            f"no live Kite price for {', '.join(unpriced)} - sized at the {as_of} close")
    _log(run, f"DualMom NAV on Kite Rs {nav:,.0f} (own cash Rs {cash:,.0f}, "
              f"{len(current)} own positions)")
    plan = R.plan(sig, current, marks, nav, reserve=C.CASH_RESERVE_RS)
    for leg in ("sells", "buys"):
        for o in plan[leg]:
            try:
                r = K.resolve(kite, o["symbol"])
                o.update(trading_symbol=r["trading_symbol"], tick_size=r["tick_size"],
                         series=r["series"], trade_for_trade=r["is_trade_for_trade"],
                         limit_preview=K.marketable_limit(o["mark"], o["side"],
                                                          C.MARKETABLE_BUFFER, r["tick_size"]))
            except Exception as e:
                o["resolve_error"] = f"{type(e).__name__}: {e}"
    # the account is shared: show what the basket does to the WHOLE account's margin
    try:
        m = K.free_margin(kite)
        need = sum(o["qty"] * o.get("limit_preview", o["mark"]) for o in plan["buys"]) - \
               sum(o["qty"] * o["mark"] for o in plan["sells"])
        plan["account_margin"] = {**m, "basket_needs": round(need, 2),
                                  "free_after": round(m["net"] - need, 2),
                                  "floor": C.MIN_FREE_MARGIN_AFTER}
        if m["net"] - need < C.MIN_FREE_MARGIN_AFTER:
            plan["blocked"].append(
                f"Kite free margin Rs {m['net']:,.0f} - basket Rs {need:,.0f} would leave "
                f"Rs {m['net'] - need:,.0f}, below the floor Rs {C.MIN_FREE_MARGIN_AFTER:,.0f}")
            plan["safe"] = False
    except Exception as e:
        plan["account_margin"] = {"error": f"{type(e).__name__}: {e}"}
        plan["blocked"].append("could not read Kite margin - refusing to size blind")
        plan["safe"] = False
    _log(run, f"plan: {len(plan['sells'])} sells / {len(plan['buys'])} buys, "
              f"buy value Rs {plan['buy_value']:,.0f}, safe={plan['safe']}")
    run["signal"], run["plan"] = sig, plan
    return run


def execute(run: dict, kite) -> dict:
    """Sells, confirmed; then buys. Refuses unless ENABLED and not DRY_RUN."""
    plan = run["plan"]
    run["orders"], run["halted"] = [], None
    if not plan.get("safe"):
        run["halted"] = "safety brake: " + "; ".join(plan.get("blocked") or [])
        _log(run, run["halted"])
        return run
    if not C.ENABLED or C.DRY_RUN:
        run["halted"] = f"DRY RUN - nothing sent (ENABLED={C.ENABLED}, DRY_RUN={C.DRY_RUN})"
        _log(run, run["halted"])
        return run
    for o in plan["sells"] + plan["buys"]:          # e.g. stop-check sells
        if not o.get("trading_symbol") and not o.get("resolve_error"):
            try:
                r = K.resolve(kite, o["symbol"])
                o.update(trading_symbol=r["trading_symbol"], tick_size=r["tick_size"],
                         series=r["series"])
            except Exception as e:
                o["resolve_error"] = f"{type(e).__name__}: {e}"
    bad_sells = [o["symbol"] for o in plan["sells"] if o.get("resolve_error")]
    if bad_sells:
        run["halted"] = f"cannot resolve a HELD position: {', '.join(bad_sells)}"
        _log(run, run["halted"])
        return run
    for o in [o for o in plan["buys"] if o.get("resolve_error")]:
        run.setdefault("dropped_buys", []).append(f"{o['symbol']} ({o['resolve_error']})")
    buys = [o for o in plan["buys"] if not o.get("resolve_error")]

    for leg, orders in (("SELL", plan["sells"]), ("BUY", buys)):
        if not orders:
            continue
        placed = []
        for o in orders:
            px = K.marketable_limit(o["mark"], o["side"], C.MARKETABLE_BUFFER, o["tick_size"])
            res = K.place_limit(kite, o["trading_symbol"], o["side"], o["qty"], px, o["tick_size"])
            rec = {**o, "limit": res["price"], "status": res["status"], "tag": res["tag"],
                   "order_id": res.get("order_id"), "reason": res.get("reason") or res.get("note"),
                   "filled_qty": 0, "avg_price": None}
            run["orders"].append(rec)
            _log(run, f"{o['side']:<4} {o['trading_symbol']:<14} x{o['qty']:<6} @ {px:>10,.2f} "
                      f"-> {res['status']}" + (f" ({rec['reason']})" if rec["reason"] else ""))
            if res["status"] == "PLACED":
                placed.append(rec)
            elif res.get("margin"):
                run["halted"] = f"margin rejection on a {leg} - halting, no retry"
                _log(run, run["halted"])
                break
        fills = K.await_all(kite, [r["order_id"] for r in placed])
        for r in placed:
            f = fills.get(r["order_id"], {})
            r["filled_qty"], r["avg_price"] = f.get("filled_qty", 0), f.get("avg_price")
            st = f.get("status", "UNKNOWN")
            r["status"] = ("FILLED" if st == "COMPLETE" and r["filled_qty"] == r["qty"]
                           else "PARTIAL" if r["filled_qty"] else f"UNFILLED:{st}")
            if st == "REJECTED":
                r["reason"] = f.get("reason")
        _log(run, f"{leg.lower()}s: {sum(1 for r in placed if r['status'] == 'FILLED')}/"
                  f"{len(orders)} filled")
        if run["halted"]:
            return run
        if leg == "SELL" and any(r["status"] != "FILLED" for r in placed):
            run["halted"] = "not every SELL filled - buys NOT sent (cash unconfirmed)"
            _log(run, run["halted"])
            return run
    return run


def complete(run: dict):
    orders = run.get("orders") or []
    counts = {}
    for o in orders:
        st = str(o.get("status") or "?").split(":")[0]
        counts[st] = counts.get(st, 0) + 1
    done = not run.get("halted") and all(o.get("status") == "FILLED" for o in orders)
    return done, {"orders": len(orders), "by_status": counts, "halted": run.get("halted")}


def save(run: dict, name: str = None) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    p = STATE / (name or f"run_{datetime.now(IST):%Y%m%d_%H%M%S}.json")
    p.write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
    return p


# ── month gate (own state file; the rule itself is month_gate.decide) ────────

def _gate_path() -> Path:
    return STATE / "month_gate.json"


def gate_state() -> dict:
    try:
        return json.loads(_gate_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def gate_mark_done(month: str, detail: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    s = gate_state()
    s["last_done_month"] = month
    s.setdefault("history", []).append({"month": month, "at": datetime.now(IST).isoformat(
        timespec="seconds"), **detail})
    s["history"] = s["history"][-24:]
    _gate_path().write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")


def filled_today(kite) -> list:
    return [o for o in K.our_orders(kite) if K._i(o.get("filled_quantity")) > 0]


def deploy_guards(kite) -> list:
    """Reasons a deploy must NOT run now (empty = clear)."""
    why = []
    if not C.ENABLED:
        why.append("dualmom_kite config.ENABLED is False")
    if C.DRY_RUN:
        why.append("dualmom_kite config.DRY_RUN is True")
    month = G.month_key(datetime.now(IST).date())
    if gate_state().get("last_done_month") == month:
        why.append(f"{month} already done on Kite (dualmom_kite_state/month_gate.json)")
    ours = K.our_orders(kite)
    n = sum(1 for o in ours if K._i(o.get("filled_quantity")) > 0)
    if n:
        why.append(f"{n} DualMom Kite order(s) already filled today - would buy twice")
    n_open = sum(1 for o in ours if str(o.get("status", "")).upper() not in K.TERMINAL)
    if n_open:
        why.append(f"{n_open} DualMom Kite order(s) still open")
    now = datetime.now(IST)
    if now.weekday() >= 5 or not ((9, 16) <= (now.hour, now.minute) <= (15, 15)):
        why.append("outside 09:16-15:15 IST on a weekday")
    return why


# ── stop check (own book only) ───────────────────────────────────────────────

def stop_breaches(kite) -> list:
    from deployment.dualmom_live import config as SC
    pos = L.own_book()["positions"]
    if not pos:
        return []
    marks = K.last_prices(kite, list(pos))
    qty = {s: int(p["qty"]) for s, p in pos.items()}
    entries = {s: float(p["avg_price"]) for s, p in pos.items()}
    for s in [s for s in pos if s not in marks]:
        marks[s] = entries[s]                       # reported as unprotected by caller
    return R.stop_breaches(qty, entries, marks, SC.STOP_LOSS_PCT)
