"""
DualMom LIVE on Kite - API. Same endpoint contract as the Kotak router
(dualmom_live_api.py) so the dashboard renders both with one set of code.

Mounted INSIDE the DualMom service under /api/dualmom/live/kite/..., which the
dashboard's existing /api/dualmom/live/{path} proxy already forwards - so adding
Kite needed no dashboard restart.

/deploy is gated: ENABLED, not DRY_RUN, typed confirmation, a preview younger
than 20 minutes, once per calendar month, never twice in a day, market hours.
"""

import asyncio
from datetime import datetime

import pytz
from fastapi import APIRouter, Body

from deployment.dualmom_live_api import _etext, _safe

router = APIRouter(prefix="/api/dualmom/live/kite", tags=["dualmom-kite"])
IST = pytz.timezone("Asia/Kolkata")
PREVIEW_MAX_AGE_MIN = 20
_last_plan = None


async def _run(fn):
    return await asyncio.get_event_loop().run_in_executor(None, fn)


def _kite():
    from deployment.dualmom_kite import kite_equity as K
    return K.client()


@router.get("/status")
async def status():
    def work():
        from deployment.dualmom_kite import config as C
        from deployment.dualmom_kite import engine as E
        from deployment.dualmom_live import config as SC
        out = {"ok": True, "broker_name": "Kite (Zerodha)", "account": C.CLIENT_ACCOUNT,
               "config": {"enabled": C.ENABLED, "dry_run": C.DRY_RUN, "top_n": SC.TOP_N,
                          "max_weight": SC.MAX_WEIGHT, "stop_loss_pct": SC.STOP_LOSS_PCT,
                          "order_tag": C.ORDER_TAG, "capital_base": C.CAPITAL_BASE,
                          "cash_reserve": C.CASH_RESERVE_RS,
                          "marketable_buffer": C.MARKETABLE_BUFFER}}
        try:
            kite = _kite()
            out["broker"] = {"connected": True}
        except Exception as e:
            out["broker"] = {"connected": False, "error": _etext(e)}
            out["can_deploy"], out["deploy_blocked_by"] = False, [_etext(e)]
            return out
        try:
            why = E.deploy_guards(kite)
        except Exception as e:
            why = [f"guard check failed: {_etext(e)}"]
        out["can_deploy"], out["deploy_blocked_by"] = not why, why
        try:
            from deployment.dualmom_kite import ledger as L
            pos = L.own_book()["positions"]
            out["holdings"] = [{"symbol": s, "qty": p["qty"], "avg_price": round(p["avg_price"], 2)}
                               for s, p in sorted(pos.items())]
            out["cash"] = L.own_cash()["cash"]
        except Exception as e:
            out["holdings"], out["cash"], out["cash_error"] = [], None, _etext(e)
        return out
    return await _run(work)


@router.get("/signal")
async def signal():
    def work():
        try:
            from deployment.dualmom_kite import engine as E
            from deployment.dualmom_live import signal_engine as S
            as_of = E.month_signal_date()
            return {"ok": True, "month_signal_date": str(as_of), **_safe(S.compute(as_of=as_of))}
        except Exception as e:
            return {"ok": False, "error": _etext(e)}
    return await _run(work)


@router.post("/plan")
async def plan(payload: dict = Body(default={})):
    """PLACES NOTHING. capital blank -> DualMom's own NAV on Kite."""
    def work():
        global _last_plan
        from deployment.dualmom_kite import engine as E
        cap = payload.get("capital")
        try:
            cap = float(cap) if cap not in (None, "") else None
        except (TypeError, ValueError):
            return {"ok": False, "error": "capital must be a number"}
        if cap is not None and cap <= 0:
            return {"ok": False, "error": "capital must be greater than zero"}
        from deployment.dualmom_kite import ledger as L0
        # Once DualMom holds Kite positions, a MANUAL preview is always a buy-only
        # top-up: selling is the monthly job's business, never a button press
        # (user, 22-Sep: "no need to sell the full portfolio").
        top_up = bool(payload.get("top_up")) or bool(L0.own_book()["positions"])
        try:
            kite = _kite()
            if top_up:
                from deployment.dualmom_kite import ledger as L
                L.capture(kite)                 # own book must include today's fills
                cap = None
            run = E.build_plan(kite, capital=cap, top_up=top_up)
        except Exception as e:
            return {"ok": False, "error": _etext(e)}
        _last_plan = run
        return {"ok": True, "run": _safe(run)}
    return await _run(work)


@router.post("/deploy")
async def deploy(payload: dict = Body(default={})):
    def work():
        from deployment.dualmom_kite import engine as E
        from deployment.dualmom_kite import ledger as L
        from deployment.dualmom_live import month_gate as G
        if payload.get("confirm") != "DEPLOY":
            return {"ok": False, "error": "confirmation text did not match - nothing sent"}
        if not _last_plan:
            return {"ok": False, "error": "no plan previewed yet - press Preview first"}
        try:
            started = datetime.fromisoformat(_last_plan["started"])
            age = (datetime.now(IST).replace(tzinfo=None) - started).total_seconds() / 60
        except Exception:
            age = None
        if age is None or age > PREVIEW_MAX_AGE_MIN:
            return {"ok": False, "error": f"the preview is {round(age, 1) if age else '?'} min old "
                                          f"- press Preview again (limit prices go stale)"}
        try:
            kite = _kite()
            top_up = bool(_last_plan.get("top_up"))
            if top_up:
                L.capture(kite)
            why = E.deploy_guards(kite, top_up=top_up)
            if why:
                return {"ok": False, "blocked": why, "error": "refused: " + "; ".join(why)}
            L.event("deploy_start", {"signal_date": _last_plan.get("month_signal_date"),
                                     "buys": len(_last_plan["plan"]["buys"]),
                                     "sells": len(_last_plan["plan"]["sells"]),
                                     "nav": _last_plan["plan"].get("nav")})
            run = E.execute(_last_plan, kite)
            E.save(run)
            run["circuit_deferred_saved"] = E.defer_circuit(run)
            try:
                L.capture(kite)
            except Exception:
                pass
            done, summary = E.complete(run)
            L.event("deploy", {**summary, "signal_date": _last_plan.get("month_signal_date")})
            if done:
                E.gate_mark_done(G.month_key(datetime.now(IST).date()),
                                 {"manual": True, "signal_date": _last_plan.get("month_signal_date"),
                                  **summary})
            return {"ok": True, "run": _safe(run), "month_recorded": done, "summary": summary,
                    "halted": run.get("halted") or None}
        except Exception as e:
            return {"ok": False, "error": _etext(e)}
    return await _run(work)


@router.get("/book")
async def book(fresh: bool = False):
    def work():
        try:
            from deployment.dualmom_kite import book as B
            return B.live(_kite(), use_cache=not fresh)
        except Exception as e:
            return {"ok": False, "error": _etext(e)}
    return await _run(work)


@router.get("/equity")
async def equity():
    def work():
        try:
            from deployment.dualmom_kite import book as B
            ser = B.equity_series()
            try:
                bk = B.live(_kite())
                if bk.get("ok") and ser["equity"]:
                    t = int(IST.localize(datetime.strptime(bk["as_of"], "%Y-%m-%d %H:%M:%S")).timestamp())
                    if t > ser["equity"][-1]["t"]:
                        ser["equity"].append({"t": t, "nav": bk["nav"], "kind": "live"})
                        peak = max(p["nav"] for p in ser["equity"])
                        ser["drawdown"].append({"t": t, "dd_pct": round((bk["nav"] / peak - 1) * 100, 4)})
                        ser["max_drawdown_pct"] = min(d["dd_pct"] for d in ser["drawdown"])
                        ser["peak_nav"] = peak
                        ser["points"] = len(ser["equity"])
            except Exception as e:
                ser["live_error"] = _etext(e)
            return {"ok": True, **ser}
        except Exception as e:
            return {"ok": False, "error": _etext(e)}
    return await _run(work)


@router.get("/ledger")
async def ledger(kind: str = "fills", limit: int = 500):
    def work():
        from deployment.dualmom_kite import ledger as L
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
    def work():
        from deployment.dualmom_kite import ledger as L
        res = L.verify_all()
        return {"ok": all(v["ok"] for v in res.values()), "chains": res}
    return await _run(work)


@router.post("/ledger/capture")
async def ledger_capture():
    def work():
        try:
            from deployment.dualmom_kite import ledger as L
            return {"ok": True, **L.capture(_kite())}
        except Exception as e:
            return {"ok": False, "error": _etext(e)}
    return await _run(work)


@router.post("/reconnect")
async def reconnect():
    """Kite: re-read today's token file. Never logs in."""
    def work():
        try:
            _kite().profile()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": _etext(e)}
    return await _run(work)
