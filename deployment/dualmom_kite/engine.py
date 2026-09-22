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


def build_plan(kite, capital: float = None, as_of=None, top_up: bool = False) -> dict:
    """Read-only. capital=None -> size against DualMom's own NAV on this account.

    top_up=True: BUY-ONLY. Targets are sized on the full own NAV exactly as a
    rebalance would, but no sell is ever sent - only the shortfalls are bought,
    largest first, while they fit in DualMom's own cash. Used 22-Sep to finish a
    basket that was partly rejected, without touching what was already bought."""
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
    if top_up:
        run["top_up"] = True
        plan["dropped_sells"] = [{"symbol": o["symbol"], "qty": o["qty"]} for o in plan["sells"]]
        plan["sells"] = []
        plan["blocked"] = [b for b in plan["blocked"] if "turnover" not in b]
    for leg in ("sells", "buys"):
        for o in plan[leg]:
            try:
                r = K.resolve(kite, o["symbol"])
                o.update(trading_symbol=r["trading_symbol"], tick_size=r["tick_size"],
                         series=r["series"], trade_for_trade=r["is_trade_for_trade"])
            except Exception as e:
                o["resolve_error"] = f"{type(e).__name__}: {e}"
    _price_legs(kite, plan)
    # A BUY whose marketable limit would sit above today's UPPER CIRCUIT is not sent
    # today (user rule, 22-Sep): the quantity is fixed now and the order goes in the
    # next trading day (pending_circuit.json, job dmk_pending_circuit 09:25). Its
    # cash stays reserved so the other buys cannot spend it.
    plan["circuit_deferred"] = [_circuit_item(o) for o in plan["buys"] if _at_upper(o)]
    plan["buys"] = [o for o in plan["buys"] if not _at_upper(o)]
    reserved = sum(d["value_at_limit"] for d in plan["circuit_deferred"])
    if plan["circuit_deferred"]:
        _log(run, "at upper circuit - next day: " + ", ".join(
            f"{d['symbol']} x{d['qty']}" for d in plan["circuit_deferred"]))
    if top_up:
        # fit the shortfalls into DualMom's OWN cash, at the limit (worst) price
        left, keep, deferred = cash - reserved, [], []
        for o in sorted(plan["buys"], key=lambda x: -x["value"]):
            cost = o["qty"] * o.get("limit_preview", o["mark"])
            if o.get("resolve_error") or cost <= left:
                keep.append(o)
                left -= 0 if o.get("resolve_error") else cost
            else:
                deferred.append({"symbol": o["symbol"], "qty": o["qty"], "needs": round(cost, 2)})
        plan["buys"], plan["deferred"] = keep, deferred
        plan["buy_value"] = round(sum(o["qty"] * o["mark"] for o in keep), 2)
        plan["n_orders"] = len(keep)
        plan["own_cash_after"] = round(left, 2)
        if deferred:
            _log(run, f"top-up: {len(deferred)} buy(s) do not fit in own cash: "
                      + ", ".join(d["symbol"] for d in deferred))
        plan["safe"] = not plan["blocked"]
    # the account is shared: show what the basket does to the WHOLE account's margin
    try:
        m = K.free_margin(kite)
        need = sum(o["qty"] * o.get("limit_preview", o["mark"]) for o in plan["buys"]) - \
               sum(o["qty"] * o["mark"] for o in plan["sells"])
        plan["account_margin"] = {**m, "basket_needs": round(need, 2),
                                  "free_after": round(m["net"] - need, 2),
                                  "floor": C.MIN_FREE_MARGIN_AFTER}
        # delivery buys are paid from CASH (live_balance) - collateral cannot pay for CNC
        if need > 0 and m.get("live_balance") is not None and need > m["live_balance"]:
            plan["blocked"].append(
                f"basket needs Rs {need:,.0f} cash but Kite has only Rs {m['live_balance']:,.0f} "
                f"cash (live_balance); collateral cannot pay for delivery buys")
            plan["safe"] = False
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


def _at_upper(o) -> bool:
    c = o.get("circuit_clamped")
    return bool(o.get("side") == "BUY" and c and c.get("upper") and c["from"] > c["upper"])


def _circuit_item(o) -> dict:
    return {"symbol": o["symbol"], "trading_symbol": o.get("trading_symbol"),
            "tick_size": o.get("tick_size"), "qty": int(o["qty"]), "mark": o["mark"],
            "upper_circuit": o["circuit_clamped"]["upper"],
            "value_at_limit": round(o["qty"] * o["circuit_clamped"]["from"], 2),
            "reason": o.get("reason"), "target_qty": o.get("target_qty"),
            "first_seen": datetime.now(IST).strftime("%Y-%m-%d"), "attempts": 0}


def _price_legs(kite, plan):
    """Marketable limit per leg, clamped inside today's circuit band."""
    legs = [o for o in plan["sells"] + plan["buys"] if o.get("trading_symbol")]
    q = K.quotes(kite, [o["trading_symbol"] for o in legs]) if legs else {}
    for o in legs:
        px = K.marketable_limit(o["mark"], o["side"], C.MARKETABLE_BUFFER, o["tick_size"])
        qq = q.get(o["trading_symbol"], {})
        cl = K.clamp_to_circuit(px, o["side"], o["tick_size"], qq)
        o["limit_preview"] = cl
        if cl != px:
            o["circuit_clamped"] = {"from": px, "to": cl, "upper": qq.get("upper_circuit"),
                                    "lower": qq.get("lower_circuit")}


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
        try:
            qb = K.quotes(kite, [o["trading_symbol"] for o in orders])
        except Exception:
            qb = {}
        for o in orders:
            px = K.marketable_limit(o["mark"], o["side"], C.MARKETABLE_BUFFER, o["tick_size"])
            q_ = qb.get(o["trading_symbol"]) or {}
            if o["side"] == "BUY" and q_.get("upper_circuit") and px > q_["upper_circuit"]:
                run.setdefault("circuit_deferred", []).append(_circuit_item(
                    {**o, "circuit_clamped": {"from": px, "upper": q_["upper_circuit"]}}))
                _log(run, f"BUY  {o['trading_symbol']:<14} x{o['qty']:<6} at upper circuit "
                          f"{q_['upper_circuit']} -> NEXT DAY")
                continue
            px = K.clamp_to_circuit(px, o["side"], o["tick_size"], q_)
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


PENDING_MAX_ATTEMPTS = 5


def _pending_path() -> Path:
    return STATE / "pending_circuit.json"


def load_pending_circuit() -> list:
    try:
        return json.loads(_pending_path().read_text(encoding="utf-8")).get("items", [])
    except Exception:
        return []


def save_pending_circuit(items: list, note: str = "") -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    by = {}
    for it in items:                       # one entry per symbol, latest qty wins
        by[it["symbol"]] = {**by.get(it["symbol"], {}), **it}
    _pending_path().write_text(json.dumps({"updated": datetime.now(IST).isoformat(
        timespec="seconds"), "note": note, "items": list(by.values())}, indent=2,
        default=str), encoding="utf-8")


def defer_circuit(run: dict) -> list:
    """Merge this run's circuit-deferred buys into pending_circuit.json."""
    new = (run.get("plan") or {}).get("circuit_deferred", []) + run.get("circuit_deferred", [])
    if new:
        save_pending_circuit(load_pending_circuit() + new, "upper-circuit buys, next trading day")
        L.event("circuit_deferred", {"items": [{"symbol": d["symbol"], "qty": d["qty"],
                                                "upper_circuit": d["upper_circuit"]} for d in new]})
    return new


def run_pending_circuit(kite) -> dict:
    """Next trading day: buy the circuit-deferred names at the quantity fixed when
    they were deferred. Still at the circuit -> wait another day (max 5 days, then
    dropped and logged). Every buy must fit DualMom's own cash AND Kite's cash."""
    items = load_pending_circuit()
    if not items:
        return {"pending": 0}
    L.capture(kite)
    if any(str(o.get("status", "")).upper() not in K.TERMINAL for o in K.our_orders(kite)):
        return {"pending": len(items), "waiting": "a DualMom order is still open"}
    q = K.quotes(kite, [i["trading_symbol"] for i in items])
    own = L.own_cash()["cash"]
    live_cash = K.free_margin(kite).get("live_balance", 0.0)
    send, keep, dropped = [], [], []
    for it in items:
        qq = q.get(it["trading_symbol"], {})
        ltp = qq.get("ltp") or 0
        lim = K.marketable_limit(ltp, "BUY", C.MARKETABLE_BUFFER, it["tick_size"]) if ltp else 0
        if not ltp or (qq.get("upper_circuit") and lim > qq["upper_circuit"]):
            it = {**it, "attempts": it.get("attempts", 0) + 1}
            (dropped if it["attempts"] >= PENDING_MAX_ATTEMPTS else keep).append(it)
            continue
        cost = it["qty"] * lim
        if cost > own or cost > live_cash:
            keep.append({**it, "attempts": it.get("attempts", 0) + 1, "last_block": "cash"})
            continue
        own -= cost
        live_cash -= cost
        send.append({"symbol": it["symbol"], "side": "BUY", "qty": it["qty"], "mark": ltp,
                     "value": round(it["qty"] * ltp, 2), "trading_symbol": it["trading_symbol"],
                     "tick_size": it["tick_size"], "target_qty": it.get("target_qty"),
                     "current_qty": None, "reason": f"deferred from {it['first_seen']} (upper circuit)"})
    run = {"started": datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds"),
           "log": [], "account": C.CLIENT_ACCOUNT, "pending_circuit": True,
           "plan": {"safe": True, "blocked": [], "sells": [], "buys": send}}
    if send:
        run = execute(run, kite)
        save(run)
        L.capture(kite)
        # anything that did not fill goes back on the list
        keep += [{**next(i for i in items if i["symbol"] == o["symbol"]),
                  "attempts": 1 + next(i for i in items if i["symbol"] == o["symbol"]).get("attempts", 0)}
                 for o in run.get("orders", []) if o.get("status") != "FILLED"]
    keep += run.get("circuit_deferred", [])
    save_pending_circuit(keep, "still pending")
    if dropped:
        L.event("circuit_dropped", {"items": dropped})
    return {"sent": len(send), "filled": sum(1 for o in run.get("orders", []) if o.get("status") == "FILLED"),
            "still_pending": [i["symbol"] for i in keep], "dropped": [i["symbol"] for i in dropped]}


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


def deploy_guards(kite, top_up: bool = False) -> list:
    """Reasons a deploy must NOT run now (empty = clear).

    top_up: same-day fills are allowed - the Kite own book is built from today's
    captured FILLS, so what was bought earlier today is already counted (unlike
    Kotak, where same-day buys are invisible in holdings). Open orders still block."""
    why = []
    if not C.ENABLED:
        why.append("dualmom_kite config.ENABLED is False")
    if C.DRY_RUN:
        why.append("dualmom_kite config.DRY_RUN is True")
    month = G.month_key(datetime.now(IST).date())
    if gate_state().get("last_done_month") == month and not top_up:
        why.append(f"{month} already done on Kite (dualmom_kite_state/month_gate.json)")
    ours = K.our_orders(kite)
    n = sum(1 for o in ours if K._i(o.get("filled_quantity")) > 0)
    if n and not top_up:
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
