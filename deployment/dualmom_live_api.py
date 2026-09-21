"""
DualMom LIVE dashboard API — a separate router, deliberately not inside main.py.

WHY A SEPARATE MODULE
    main.py is the process that runs the live Vwap Strangle. Every import here is
    lazy and guarded so a broken DualMom package can never take that process down.
    Wiring is a single `app.include_router(...)` line.

SAFETY
    Every endpoint is read-only except /deploy, which is gated FOUR ways:
        1. config.ENABLED must be True
        2. config.DRY_RUN must be False
        3. the browser must send the exact confirmation text
        4. a plan must have been previewed first (no blind deploy)
    The Kotak client here is for the DEDICATED DualMom account. kotak_auth_dm
    refuses to start if any credential matches the strangle's, so this can never
    steal the strangle's session.
"""

import asyncio

from fastapi import APIRouter, Body

router = APIRouter(prefix="/api/dualmom/live", tags=["dualmom-live"])

_client = None
_last_plan = {}


def _mods():
    from deployment.dualmom_live import config as C
    from deployment.dualmom_live import kotak_equity as K
    from deployment.dualmom_live import rebalance as R
    from deployment.dualmom_live import runner as RUN
    from deployment.dualmom_live import signal_engine as S
    return C, S, R, RUN, K


import threading as _threading
_client_lock = _threading.Lock()


def _get_client(reconnect: bool = False, validate: bool = False):
    """The ONE Kotak Rohit session for this process.

    Kotak allows a single session per account. The scheduled jobs in
    dualmom_service used to log in on their own, so a 15:25 stop check could kill
    the session the dashboard was using (or vice versa). Everything in the service
    now shares this client. validate=True makes a cheap limits() call first and
    logs in again if the session has expired.
    """
    global _client
    from deployment.dualmom_live import kotak_auth_dm as A
    with _client_lock:
        if _client is not None and validate and not reconnect:
            try:
                r = _client.limits()
                bad = isinstance(r, dict) and (r.get("error") or r.get("Error Message")
                                               or str(r.get("stCode", "")) in ("900901", "401"))
                if bad:
                    reconnect = True
            except Exception:
                reconnect = True
        if _client is None or reconnect:
            _client = A.login(verbose=False)
        return _client


PREVIEW_MAX_AGE_MIN = 20


def _plan_age_minutes(run):
    from datetime import datetime as _dt
    try:
        return round((_dt.now() - _dt.fromisoformat(run["started"])).total_seconds() / 60, 1)
    except Exception:
        return None


def rebalance_complete(run: dict):
    """(done?, summary). The month counts as DONE only if nothing halted and EVERY
    order reached FILLED.

    On 2026-09-15 all 40 orders were rejected, nothing halted (a rejection is not a
    halt), and the old rule recorded September as deployed with Rs 0 invested - so
    Deploy would then have refused to try again.
    """
    orders = run.get("orders") or []
    counts = {}
    for o in orders:
        st = str(o.get("status") or "?").split(":")[0]
        counts[st] = counts.get(st, 0) + 1
    summary = {"orders": len(orders), "by_status": counts,
               "halted": run.get("halted") or None}
    done = (not run.get("halted")) and all(o.get("status") == "FILLED" for o in orders)
    return done, summary


def _month_signal_date(S, today=None):
    """The signal date that governs THIS calendar month: the last session before
    the 1st present in the data (e.g. 31-Aug for September).

    The strategy only checks the regime at month-end and holds between them, so a
    mid-month preview must show THIS basket - not a fresh signal on yesterday's
    close, which on 11-Sep said OUT and would have shown 0 buys for a month whose
    real signal was IN."""
    from datetime import datetime as _dt
    import pytz
    today = today or _dt.now(pytz.timezone("Asia/Kolkata")).date()
    month_start = today.replace(day=1)
    dates = [x.date() for x in S.load_prices().index if x.date() < month_start]
    return max(dates) if dates else None


def _safe(o):
    """Drop broker 'raw' blobs and convert numpy scalars so this serialises."""
    try:
        import numpy as np
    except Exception:
        np = None
    if isinstance(o, dict):
        return {k: _safe(v) for k, v in o.items() if k != "raw"}
    if isinstance(o, (list, tuple)):
        return [_safe(v) for v in o]
    if np is not None:
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
    return o


def _etext(e) -> str:
    """Exception text that can't itself raise. Kotak SDK exceptions can return
    None from __str__, which turned a failed broker call into a TypeError."""
    try:
        msg = str(e)
    except Exception:
        msg = repr(getattr(e, "args", ""))
    return f"{type(e).__name__}: {msg}"


async def _run(fn):
    return await asyncio.get_event_loop().run_in_executor(None, fn)


@router.get("/status")
async def status():
    """Config gates + broker state. Places nothing."""
    def work():
        try:
            C, S, R, RUN, K = _mods()
        except Exception as e:
            return {"ok": False, "stage": "import", "error": f"{_etext(e)}"}

        blocked = []
        if not C.ENABLED:
            blocked.append("config.ENABLED is False")
        if C.DRY_RUN:
            blocked.append("config.DRY_RUN is True")

        out = {
            "ok": True,
            "config": {
                "enabled": C.ENABLED,
                "dry_run": C.DRY_RUN,
                "top_n": C.TOP_N,
                "max_weight": C.MAX_WEIGHT,
                "stop_loss_pct": C.STOP_LOSS_PCT,
                "order_tag": C.ORDER_TAG,
                "sizing_slippage": C.SIZING_SLIPPAGE,
                "marketable_buffer": C.MARKETABLE_BUFFER,
            },
            "can_deploy": not blocked,
            "deploy_blocked_by": blocked,
        }
        try:
            client = _get_client()
            out["broker"] = {"connected": True}
        except Exception as e:
            out["broker"] = {"connected": False, "error": f"{_etext(e)}"}
            return out

        try:
            h = K.holdings(client)
            out["holdings"] = [
                {"symbol": s, "qty": v["qty"], "avg_price": round(v["avg_price"], 2)}
                for s, v in sorted(h.items())
            ]
        except Exception as e:
            out["holdings"] = []
            out["holdings_error"] = f"{_etext(e)}"

        try:
            out["cash"] = round(K.cash_available(client), 2)
        except Exception as e:
            out["cash"] = None
            out["cash_error"] = f"{_etext(e)}"
        return out
    return await _run(work)


@router.get("/signal")
async def signal():
    """THIS MONTH's signal + target basket (signal on the last session before the 1st)."""
    def work():
        try:
            C, S, R, RUN, K = _mods()
            as_of = _month_signal_date(S)
            return {"ok": True, "month_signal_date": str(as_of), **_safe(S.compute(as_of=as_of))}
        except Exception as e:
            return {"ok": False, "error": f"{_etext(e)}"}
    return await _run(work)


@router.post("/plan")
async def plan(payload: dict = Body(default={})):
    """Build the rebalance plan for a capital amount. PLACES NOTHING.

    capital omitted/blank -> size against the broker's real NAV.
    """
    def work():
        global _last_plan
        try:
            C, S, R, RUN, K = _mods()
        except Exception as e:
            return {"ok": False, "error": f"{_etext(e)}"}

        cap = payload.get("capital")
        try:
            cap = float(cap) if cap not in (None, "") else None
        except (TypeError, ValueError):
            return {"ok": False, "error": "capital must be a number"}
        if cap is not None and cap <= 0:
            return {"ok": False, "error": "capital must be greater than zero"}

        try:
            client = _get_client(validate=True)
        except Exception as e:
            return {"ok": False, "error": f"broker login failed: {_etext(e)}"}

        # This month's basket (signal on last month's final close), sized at LIVE
        # Kotak prices. Sizing a mid-month entry at the month-end close would get
        # every share count wrong by however far the stock has moved since.
        try:
            as_of = _month_signal_date(S)
            sig = S.compute(as_of=as_of)
            syms = [h["symbol"] for h in sig.get("holdings", [])]
            live = K.last_prices(client, syms) if syms else {}
            unpriced = [s for s in syms if s not in live]
            marks = {h["symbol"]: live.get(h["symbol"], h["price"]) for h in sig.get("holdings", [])}
            run = RUN.build_plan(client=client, nav=cap, marks=marks or None, as_of=as_of)
            run["month_signal_date"] = str(as_of)
            run["live_priced"] = len(live)
            if unpriced:
                run.setdefault("warnings", []).append(
                    f"no live price for {', '.join(unpriced)} - sized at the {as_of} close")
        except Exception as e:
            return {"ok": False, "error": f"{_etext(e)}"}

        # Resolve every leg so the UI shows EXACTLY what would be sent — the real
        # trading symbol (HFCL-BE, not HFCL), the instrument's own tick, and the
        # marketable limit that tick produces.
        p = run.get("plan", {})
        for leg in ("sells", "buys"):
            for o in p.get(leg, []):
                try:
                    r = K.resolve(client, o["symbol"])
                    o["trading_symbol"] = r["trading_symbol"]
                    o["tick_size"] = r["tick_size"]
                    o["series"] = r["series"]
                    o["trade_for_trade"] = r["is_trade_for_trade"]
                    o["limit_preview"] = K.marketable_limit(
                        o["mark"], o["side"], C.MARKETABLE_BUFFER, r["tick_size"])
                except Exception as e:
                    o["resolve_error"] = f"{_etext(e)}"

        _last_plan = run
        return {"ok": True, "run": _safe(run)}
    return await _run(work)


@router.post("/deploy")
async def deploy(payload: dict = Body(default={})):
    """Place the previewed plan. Refuses unless every gate is open."""
    def work():
        try:
            C, S, R, RUN, K = _mods()
        except Exception as e:
            return {"ok": False, "error": f"{_etext(e)}"}

        if payload.get("confirm") != "DEPLOY":
            return {"ok": False, "error": "confirmation text did not match — nothing sent"}
        if not _last_plan:
            return {"ok": False, "error": "no plan previewed yet — press Preview first"}

        blocked = []
        if not C.ENABLED:
            blocked.append("config.ENABLED is False")
        if C.DRY_RUN:
            blocked.append("config.DRY_RUN is True")
        if blocked:
            return {"ok": False, "blocked": blocked,
                    "error": "refused: " + "; ".join(blocked)}

        # one entry per calendar month - the manual button and the 09:20 job share
        # the same month_gate record, so neither can buy the basket twice
        from datetime import datetime as _dt
        import pytz
        from deployment.dualmom_live import month_gate as G
        month = G.month_key(_dt.now(pytz.timezone("Asia/Kolkata")).date())
        if G.load_state().get("last_done_month") == month:
            return {"ok": False, "error": f"{month} is already deployed - refusing a second "
                                          f"entry (see dualmom_live_state/month_gate.json)"}

        age = _plan_age_minutes(_last_plan)
        if age is None or age > PREVIEW_MAX_AGE_MIN:
            return {"ok": False, "error": f"the preview is {age if age is not None else '?'} min "
                                          f"old - press Preview again (limit prices go stale)"}

        try:
            client = _get_client(validate=True)
            # Same-day buys sit in POSITIONS, not holdings, until settlement, so the
            # plan cannot see them. Re-running after anything filled today would buy
            # those names a second time.
            filled_today = [o for o in K.strategy_orders(client, C.ORDER_TAG)
                            if K._num(K._dig(o, "fldQty", "filledQuantity")) > 0]
            if filled_today:
                return {"ok": False, "error": (
                    f"{len(filled_today)} DualMom order(s) already FILLED today - refusing "
                    f"to re-run the basket (it would buy them twice). Top-up tomorrow, "
                    f"once today's buys show in holdings.")}

            run = RUN.execute(_last_plan, client)
            try:
                RUN.save(run)
            except Exception:
                pass
            # into the client ledger immediately, not at the next scheduled capture
            try:
                from deployment.dualmom_live import ledger as L
                L.event("deploy", {"orders": len(run.get("orders", [])),
                                   "halted": run.get("halted"),
                                   "signal_date": _last_plan.get("month_signal_date")})
                L.capture(client)
            except Exception:
                pass
            done, summary = rebalance_complete(run)
            if done:
                G.mark_done(month, {"manual": True,
                                    "signal_date": _last_plan.get("month_signal_date"),
                                    **summary})
            return {"ok": True, "run": _safe(run), "month_recorded": done,
                    "summary": summary, "halted": run.get("halted") or None}
        except Exception as e:
            return {"ok": False, "error": f"{_etext(e)}"}
    return await _run(work)


@router.get("/book")
async def book(fresh: bool = False):
    """Live client book from the broker, cross-checked against the ledger."""
    def work():
        try:
            from deployment.dualmom_live import book as B
            return B.live(_get_client(validate=True), use_cache=not fresh)
        except Exception as e:
            return {"ok": False, "error": f"{_etext(e)}"}
    return await _run(work)


@router.get("/equity")
async def equity():
    """NAV curve + drawdown since inception, with the live NAV as the last point."""
    def work():
        try:
            from deployment.dualmom_live import book as B
            ser = B.equity_series()
            try:
                bk = B.live(_get_client(validate=True))
                if bk.get("ok"):
                    import pytz
                    from datetime import datetime as _dt
                    t = int(pytz.timezone("Asia/Kolkata").localize(
                        _dt.strptime(bk["as_of"], "%Y-%m-%d %H:%M:%S")).timestamp())
                    if not ser["equity"] or t > ser["equity"][-1]["t"]:
                        ser["equity"].append({"t": t, "nav": bk["nav"], "kind": "live"})
                        peak = max(p["nav"] for p in ser["equity"])
                        ser["drawdown"].append({"t": t, "dd_pct": round((bk["nav"] / peak - 1) * 100, 4)})
                        ser["max_drawdown_pct"] = min(d["dd_pct"] for d in ser["drawdown"])
                        ser["peak_nav"] = peak
            except Exception as e:
                ser["live_error"] = f"{_etext(e)}"
            return {"ok": True, **ser}
        except Exception as e:
            return {"ok": False, "error": f"{_etext(e)}"}
    return await _run(work)


@router.get("/ledger")
async def ledger(kind: str = "fills", limit: int = 500):
    """Recent ledger records (raw broker payload omitted for size)."""
    def work():
        from deployment.dualmom_live import ledger as L
        name = {"fills": "fills.jsonl", "orders": "orders.jsonl",
                "attempts": "attempts.jsonl", "events": "events.jsonl"}.get(kind)
        if not name:
            return {"ok": False, "error": "kind must be fills|orders|attempts|events"}
        recs = L._read(name)
        slim = [{k: v for k, v in r.items() if k != "raw"} for r in recs[-max(1, min(limit, 5000)):]]
        return {"ok": True, "kind": kind, "total": len(recs), "records": slim}
    return await _run(work)


@router.get("/ledger/verify")
async def ledger_verify():
    """Recompute every hash chain - proves no ledger record was altered or removed."""
    def work():
        from deployment.dualmom_live import ledger as L
        res = L.verify_all()
        return {"ok": all(v["ok"] for v in res.values()), "chains": res}
    return await _run(work)


@router.post("/ledger/capture")
async def ledger_capture():
    """Pull orders + fills from the broker into the ledger now (idempotent)."""
    def work():
        try:
            from deployment.dualmom_live import ledger as L
            return {"ok": True, **L.capture(_get_client(validate=True))}
        except Exception as e:
            return {"ok": False, "error": f"{_etext(e)}"}
    return await _run(work)


@router.post("/reconnect")
async def reconnect():
    """Force a fresh Kotak login for the DualMom account."""
    def work():
        try:
            _get_client(reconnect=True)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{_etext(e)}"}
    return await _run(work)
