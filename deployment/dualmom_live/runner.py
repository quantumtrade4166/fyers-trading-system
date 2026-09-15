"""
dualmom_live/runner.py — orchestration. Signal -> NAV -> plan -> (optionally) orders.

⚠️ INERT BY DEFAULT. `config.ENABLED = False` and `config.DRY_RUN = True`, and
this module NEVER logs in — a Kotak client must be injected. See the session
hazard note in kotak_equity.py: the Vwap Strangle holds the account's only Kotak
session during market hours.

Execution order is dictated by T+1 settlement, not preference:
    1. place ALL sells, wait for confirmation
    2. read the cash that actually settled
    3. size and place buys against THAT number

Sizing buys off expected proceeds is how you get a wave of margin rejections at
15:25 on rebalance day.

Failure policy (from [[Live Order Safety]]):
    - a margin rejection HALTS the run; it does not retry into the same wall
    - only order_status() may mark an order done; 'placed' is not 'filled'
    - every order carries the `dualmom` tag so the book is ours, never the
      broker's netted position
"""

import json
from datetime import datetime
from pathlib import Path

from . import config as C
from . import kotak_equity as K
from . import rebalance as R
from . import signal_engine as S

STATE = Path(__file__).resolve().parents[1] / C.STATE_DIR


def _log(run: dict, msg: str):
    stamp = datetime.now().strftime("%H:%M:%S")
    run["log"].append(f"{stamp} {msg}")
    print(f"  [dualmom] {msg}", flush=True)


def build_plan(client=None, marks: dict = None, nav: float = None,
               current: dict = None, as_of=None) -> dict:
    """Signal + current book -> order plan. Read-only; places nothing.

    Pass client=None with explicit marks/nav/current to run fully offline.
    """
    run = {"started": datetime.now().isoformat(timespec="seconds"), "log": []}
    sig = S.compute(as_of=as_of)
    _log(run, f"signal {sig['signal']} on {sig['date']} "
              f"(nifty {sig['nifty_close']:,.2f} vs MA {sig['nifty_ma']:,.2f}, "
              f"{sig['gap_pct']:+.2f}%), universe {sig['universe_valid']}")

    if marks is None:
        marks = {h["symbol"]: h["price"] for h in sig["holdings"]}

    if current is None:
        if client is None:
            raise ValueError("need either `current` holdings or a Kotak client")
        current = {s: v["qty"] for s, v in K.holdings(client).items()}
        _log(run, f"broker holdings: {len(current)} names")

    if nav is None:
        if client is None:
            raise ValueError("need either `nav` or a Kotak client")
        av = K.account_value(client, marks)
        nav = av["nav"]
        _log(run, f"NAV Rs {nav:,.0f} (holdings Rs {av['market_value']:,.0f} "
                  f"+ cash Rs {av['cash']:,.0f})")

    plan = R.plan(sig, current, marks, nav)
    _log(run, f"plan: {len(plan['sells'])} sells / {len(plan['buys'])} buys, "
              f"turnover {plan['turnover_pct']:.1f}%, "
              f"{plan['target_names']} names targeted")
    for b in plan["blocked"]:
        _log(run, f"BLOCKED: {b}")
    run["signal"], run["plan"] = sig, plan
    return run


def execute(run: dict, client, force: bool = False) -> dict:
    """Place the plan. Refuses unless ENABLED and not DRY_RUN (or force=True).

    `force` exists for a SUPERVISED first run and must never be wired to a
    scheduler.
    """
    plan = run["plan"]
    run["orders"] = []
    run["halted"] = None

    if not plan["safe"]:
        run["halted"] = f"safety brake: {'; '.join(plan['blocked'])}"
        _log(run, run["halted"])
        return run
    if not force and (not C.ENABLED or C.DRY_RUN):
        run["halted"] = ("DRY RUN — nothing sent "
                         f"(ENABLED={C.ENABLED}, DRY_RUN={C.DRY_RUN})")
        _log(run, run["halted"])
        return run
    if client is None:
        run["halted"] = "no Kotak client injected"
        _log(run, run["halted"])
        return run

    # ---- 0. resolve EVERY name before sending anything ----
    #
    # Without this the order carries the bare symbol ('HFCL') instead of Kotak's
    # trading symbol ('HFCL-BE'), and prices round to a default 0.05 tick when the
    # instrument's real tick can be 5.00 (POWERINDIA). Both are exchange-level
    # rejections. Verified against the live scrip master 2026-09-08.
    #
    # Asymmetric on purpose: a BUY we cannot resolve is simply not bought -- the
    # cash stays idle and is reported. A SELL we cannot resolve means we HOLD a
    # position we cannot exit, which must stop the run and be seen by a human.
    unresolved_sells, dropped_buys, tft = [], [], []
    for leg, orders in (("sells", plan["sells"]), ("buys", plan["buys"])):
        for o in list(orders):
            try:
                r = K.resolve(client, o["symbol"])
                o["trading_symbol"] = r["trading_symbol"]
                o["tick_size"] = r["tick_size"]
                o["series"] = r["series"]
                if r["is_trade_for_trade"]:
                    tft.append(o["symbol"])
            except Exception as e:
                reason = f"{type(e).__name__}: {e}"
                if leg == "sells":
                    unresolved_sells.append(f"{o['symbol']} ({reason})")
                else:
                    o["dropped"] = reason
                    dropped_buys.append(f"{o['symbol']} ({reason})")
                    orders.remove(o)
    if tft:
        _log(run, f"trade-for-trade (BE) names, compulsory delivery: {', '.join(tft)}")
    if dropped_buys:
        run.setdefault("dropped_buys", []).extend(dropped_buys)
        _log(run, f"DROPPED {len(dropped_buys)} unresolvable buy(s): {'; '.join(dropped_buys)}")
    if unresolved_sells:
        run["halted"] = ("cannot resolve a HELD position — refusing to trade a "
                         f"partial book: {'; '.join(unresolved_sells)}")
        _log(run, run["halted"])
        return run

    # ---- 1. sells first ----
    realised = 0.0
    for o in plan["sells"]:
        res = _send(run, client, o)
        if res.get("margin"):
            run["halted"] = "margin rejection on a SELL — halting"
            _log(run, run["halted"])
            return run
        realised += res.get("filled_qty", 0) * (res.get("avg_price") or o["mark"])
    _log(run, f"sells done, realised Rs {realised:,.0f}")

    # ---- 2. re-read what actually settled; never assume ----
    cash = K.cash_available(client)
    _log(run, f"broker cash after sells: Rs {cash:,.0f}")

    # ---- 3. buys, sized against real cash ----
    spent = 0.0
    for o in plan["buys"]:
        # worst-case cost uses the LIMIT cap, so a buy is never sent
        # that settled cash cannot cover even if it fills at the cap
        cost = o["qty"] * o["mark"] * (1 + C.MARKETABLE_BUFFER)
        if spent + cost > cash:
            o["deferred"] = "insufficient settled cash — defer to next session"
            _log(run, f"DEFER {o['symbol']} x{o['qty']} (needs Rs {cost:,.0f})")
            continue
        res = _send(run, client, o)
        if res.get("margin"):
            run["halted"] = "margin rejection on a BUY — halting, no retry"
            _log(run, run["halted"])
            return run
        spent += res.get("filled_qty", 0) * (res.get("avg_price") or o["mark"])
    _log(run, f"buys done, deployed Rs {spent:,.0f}")
    return run


def _send(run: dict, client, o: dict) -> dict:
    """One order, to a terminal state. Records only what the broker confirmed."""
    # the instrument's own tick — a 5.00-tick name priced to 0.05 is rejected
    tick = float(o.get("tick_size") or 0.05)
    px = K.marketable_limit(o["mark"], o["side"], C.MARKETABLE_BUFFER, tick)
    res = K.place_limit(client, o.get("trading_symbol", o["symbol"]),
                        o["side"], o["qty"], px, tick=tick, tag=C.ORDER_TAG)
    rec = {**o, "limit": px, "status": res["status"], "tag": res.get("tag"),
           "order_id": res["order_id"], "reason": res.get("reason"),
           "margin": res.get("margin", False), "filled_qty": 0, "avg_price": None}

    if res["status"] == "PLACED":
        fill = K.await_fill(client, res["order_id"],
                            C.ORDER_POLL_TIMEOUT, C.ORDER_POLL_SECONDS)
        rec.update(filled_qty=fill["filled_qty"], avg_price=fill["avg_price"],
                   status="FILLED" if fill["ok"] else f"UNFILLED:{fill['status']}")
        if fill["filled_qty"] and fill["filled_qty"] < o["qty"]:
            rec["status"] = "PARTIAL"

    _log(run, f"{o['side']:<4} {o['symbol']:<12} x{o['qty']:<6} @ {px:>10,.2f} "
              f"-> {rec['status']}" + (f"  ({rec['reason']})" if rec["reason"] else ""))
    run["orders"].append(rec)
    return rec


def save(run: dict, name: str = None) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    name = name or f"run_{datetime.now():%Y%m%d_%H%M%S}.json"
    p = STATE / name
    p.write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
    return p



# ── pending plan ─────────────────────────────────────────────────────────────
#
# The month-end signal is computed at 15:20 but executed at 09:20 the NEXT
# morning, so the plan has to survive overnight AND survive a restart. It is a
# single file, deliberately not a queue: two pending plans would mean two
# rebalances, and clear_pending() is called only after execute() returns.

PENDING = STATE / "pending_plan.json"


def save_pending(run: dict) -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
    return PENDING


def load_pending() -> dict | None:
    if not PENDING.exists():
        return None
    try:
        return json.loads(PENDING.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_pending() -> None:
    """Archive rather than delete — a plan that was executed is evidence."""
    if not PENDING.exists():
        return
    STATE.mkdir(parents=True, exist_ok=True)
    PENDING.replace(STATE / f"executed_{datetime.now():%Y%m%d_%H%M%S}.json")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print("DualMom live runner — DRY RUN, no broker contact\n")
    # Offline demo: pretend we hold nothing and have Rs 10L in cash.
    run = build_plan(client=None, nav=1_000_000, current={})
    p = run["plan"]
    print(f"\n  orders: {len(p['sells'])} sells / {len(p['buys'])} buys")
    print(f"  buy value Rs {p['buy_value']:,.0f} of NAV Rs {p['nav']:,.0f}"
          f"  ({p['buy_value']/p['nav']*100:.1f}% deployed)")
    print(f"  safe={p['safe']}  blocked={p['blocked']}")
    print("\n  first 8 buys:")
    for o in p["buys"][:8]:
        print(f"    {o['symbol']:<13} x{o['qty']:<6} @ Rs {o['mark']:>10,.2f} "
              f"= Rs {o['value']:>10,.0f}")
    print(f"\n  saved -> {save(run, 'dry_run_demo.json')}")
