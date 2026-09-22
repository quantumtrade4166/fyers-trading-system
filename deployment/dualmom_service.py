"""
DualMom LIVE — a STANDALONE service, deliberately its own process.

WHY IT IS SEPARATE
    It used to live inside the dashboard, which also hosts the live Vwap Strangle:
    its WebSocket feed, the per-minute signal check, the 2-minute capture that
    supervises the V2 engine, and order routing. Changing one line of DualMom
    meant restarting all of that — and mid-session the strangle would go blind
    for the ~60-90s the dashboard takes to reload 500 stocks. If its entry trigger
    or the MTM stop fired in that window it would simply be missed.

    So DualMom now runs on its own port. Restart it whenever you like, including
    while the market is open: the strangle never notices. Same pattern the BTC
    engine already uses.

RUN
    .venv\\Scripts\\python.exe -m deployment.dualmom_service            # port 8010
    .venv\\Scripts\\python.exe -m deployment.dualmom_service --no-sched # API only

The dashboard proxies /api/dualmom/live/* here, so the browser URLs never change
and the proxy itself never needs editing again.
"""

import argparse
import os
import socket
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / "deployment" / ".env")

import pytz
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from deployment.dualmom_live_api import router as live_router, _etext

IST = pytz.timezone("Asia/Kolkata")
PORT = int(os.getenv("DUALMOM_PORT", "8010"))

# Own singleton port — must NOT collide with the strangle's 47651/47652.
PORT_DUALMOM = 47661

_lock_sock: socket.socket | None = None
_sched: BackgroundScheduler | None = None


def _acquire_lock(port: int) -> bool:
    """One DualMom service only. Two would double every order."""
    global _lock_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):      # Windows: true exclusivity
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        _lock_sock = s
        return True
    except OSError:
        s.close()
        return False


def _log(msg: str):
    print(f"  [dualmom-svc {datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


# ── scheduled jobs ───────────────────────────────────────────────────────────
#
# Every job is a no-op for ORDERS while config.ENABLED is False / DRY_RUN is True
# — they compute, log, and save, so the whole schedule can be watched for a full
# month before anything is armed.

def _client():
    """The SAME Kotak Rohit session the dashboard API uses (one session per account).
    Logging in separately here could silently end the session the UI is using."""
    from deployment.dualmom_live_api import _get_client
    return _get_client(validate=True)


def _job_data_refresh():
    """16:10 daily — bring the Nifty 500 parquets up to today's close.

    Runs BEFORE the month-end signal (18:30) on purpose: the signal must see the
    month-end close itself, and stale parquets would rank last month's momentum.
    """
    try:
        from deployment.dualmom_live import data_refresh as D
        _log("data refresh: starting")
        rep = D.refresh(verbose=False)
        _log(f"data refresh done: {rep['counts']} index_last={rep.get('index_last')}")
        if rep.get("series_changed"):
            _log(f"SERIES CHANGED: {', '.join(rep['series_changed'])}")
        if rep.get("readjusted"):
            _log(f"RE-ADJUSTED (history rewritten): {', '.join(rep['readjusted'])}")
        if rep.get("failed"):
            _log(f"data refresh had {len(rep['failed'])} failure(s): {rep['failed'][:5]}")
    except Exception as e:
        _log(f"data refresh FAILED: {_etext(e)}")


def _job_monthly_rebalance():
    """09:20 Mon-Fri - rebalance ONCE per calendar month, on its first session.

    Replaces the old 18:30 "is today month-end?" job, which misread live data as
    month-end EVERY day (fired on Fri 11-Sep). See month_gate.py for the rule.
    Retried automatically on later mornings if the exchange was closed.
    """
    try:
        from deployment.dualmom_live import month_gate as G
        from deployment.dualmom_live import runner as RUN
        from deployment.dualmom_live import signal_engine as S

        today = datetime.now(IST).date()
        state = G.load_state()
        if state.get("last_done_month") == G.month_key(today):
            return
        prices = S.load_prices()
        dates = {x.date() for x in prices.index}
        # decide WITHOUT touching the broker first; only log in if we may trade
        d = G.decide(today, dates, state.get("last_done_month"), market_open=True)
        if d["action"] != "run":
            _log(f"monthly rebalance {d['action'].upper()}: {d['reason']}")
            return
        client = _client()
        try:
            from deployment.dualmom_live import data_refresh as D
            fy = D._connect()
        except Exception:
            fy = None
        is_open = G.market_traded_today(client, today, fyers=fy)
        if is_open is not True:
            d = G.decide(today, dates, state.get("last_done_month"), market_open=is_open)
            _log(f"monthly rebalance {d['action'].upper()}: {d['reason']}")
            return

        _log(f"monthly rebalance: {d['reason']}")
        run = RUN.build_plan(client=client, as_of=d["signal_date"])
        RUN.save(run, f"signal_{d['signal_date']:%Y%m%d}.json")
        p = run.get("plan", {})
        _log(f"plan: signal {run['signal']['signal']} on {run['signal']['date']}, "
             f"{len(p.get('buys', []))} buys / {len(p.get('sells', []))} sells, "
             f"safe={p.get('safe')}")
        run = RUN.execute(run, client)
        RUN.save(run, f"run_{datetime.now(IST):%Y%m%d_%H%M%S}.json")
        from deployment.dualmom_live_api import rebalance_complete
        done, summary = rebalance_complete(run)
        if not done:
            # halted, or any order REJECTED / UNFILLED / PARTIAL: do NOT record the
            # month. (A rejection is not a halt - on 2026-09-15 all 40 orders were
            # rejected and the old rule still recorded the month as done.)
            _log(f"monthly rebalance NOT complete, month not recorded: {summary}")
            return
        G.mark_done(G.month_key(today), {"signal_date": str(d["signal_date"]), **summary})
        _log(f"monthly rebalance DONE for {G.month_key(today)}: {summary}")
    except Exception as e:
        _log(f"monthly rebalance FAILED: {_etext(e)}")


_hb = {"wall": None, "mono": None}


def _job_heartbeat():
    """Every 60s. Two jobs in one:

    1. It forces the scheduler loop to wake at least once a minute. APScheduler
       sleeps on a MONOTONIC timer until its next job, and that timer does not
       advance while the VM is paused. On 14-Sep the host paused the VPS 01:22 -
       09:19 IST; afterwards the scheduler sat blind until 17:17 and skipped the
       09:20, 15:25 and 16:10 jobs as "missed". With a 60s job it re-checks the
       wall clock within a minute of waking.
    2. It records the pause: wall time jumping far ahead of monotonic time is the
       signature of a suspended VM.
    """
    import time as _t
    wall, mono = _t.time(), _t.monotonic()
    if _hb["wall"] is not None:
        gap = (wall - _hb["wall"]) - (mono - _hb["mono"])
        if gap > 120:
            _log(f"VM PAUSE DETECTED: wall clock jumped {gap/60:.1f} min more than the "
                 f"process ran - the host suspended this machine")
    _hb["wall"], _hb["mono"] = wall, mono


def _job_stop_check():
    """15:25 daily — the -35% stop. Without this job the stop NEVER fires.

    This is the job that turned DualMom from a monthly strategy into one needing
    daily unattended sell capability, so it is deliberately loud in the log.
    """
    try:
        from deployment.dualmom_live import config as C
        from deployment.dualmom_live import kotak_equity as K
        from deployment.dualmom_live import rebalance as R
        from deployment.dualmom_live import runner as RUN
        from deployment.dualmom_live import book as B
        client = _client()
        # Settled holdings AND today's unsettled buys. holdings() alone is empty on
        # the day of purchase (T+1 settlement), which would leave a freshly bought
        # basket outside the stop for its first session.
        held = {s: {"qty": p["qty"], "avg_price": p["avg_price"]}
                for s, p in B.broker_positions(client).items() if p["qty"] > 0}
        if not held:
            return
        # ONE batched quotes() call for the whole book.
        marks = K.last_prices(client, list(held))
        unpriced = [s for s in held if s not in marks]
        if unpriced:
            # Falling back to the entry price means that name CANNOT breach, i.e.
            # it is silently unprotected. That must never pass quietly — it is
            # exactly how a stop you believe in turns out never to fire.
            _log(f"WARNING: no mark for {len(unpriced)} name(s) — UNPROTECTED "
                 f"this run: {', '.join(unpriced)}")
            for s in unpriced:
                marks[s] = held[s]["avg_price"]
        entries = {s: v["avg_price"] for s, v in held.items()}
        qty = {s: v["qty"] for s, v in held.items()}
        breaches = R.stop_breaches(qty, entries, marks)
        if not breaches:
            _log(f"stop check: {len(held)} names, none below -{C.STOP_LOSS_PCT:.0%}")
            return
        names = ", ".join(o["symbol"] for o in breaches)
        _log(f"STOP BREACH on {len(breaches)} name(s): {names}")
        run = {"plan": {"safe": True, "blocked": [], "sells": breaches, "buys": []},
               "log": [], "started": datetime.now(IST).isoformat()}
        run = RUN.execute(run, client)
        RUN.save(run, f"stop_{datetime.now(IST):%Y%m%d_%H%M%S}.json")
        _log(f"stop run finished — halted={run.get('halted')}")
        try:
            from deployment.dualmom_live import ledger as L
            L.event("stop_run", {"names": [o["symbol"] for o in breaches],
                                 "halted": run.get("halted")})
            L.capture(client)
        except Exception as e:
            _log(f"ledger capture after stop FAILED: {_etext(e)}")
    except Exception as e:
        _log(f"stop check FAILED: {_etext(e)}")


def _market_open_now() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 35)


def _job_ledger_capture():
    """Every few minutes in market hours (and once at startup): orders + fills
    from the broker into the client ledger. Idempotent."""
    try:
        from deployment.dualmom_live import ledger as L
        res = L.capture(_client())
        if res.get("orders") or res.get("fills") or any(k.endswith("_error") for k in res):
            _log(f"ledger capture: {res}")
    except Exception as e:
        _log(f"ledger capture FAILED: {_etext(e)}")


def _job_intraday_nav():
    """Every few minutes while the market is open: one NAV point for the curve."""
    if not _market_open_now():
        return
    try:
        from deployment.dualmom_live import book as B
        from deployment.dualmom_live import ledger as L
        bk = B.live(_client(), use_cache=False)
        if bk.get("positions"):
            L.record_intraday(B.snapshot_row(bk))
    except Exception as e:
        _log(f"intraday NAV FAILED: {_etext(e)}")


def _job_eod_snapshot():
    """15:40 on TRADING days: final capture, book, reconciliation, NAV row, chain check."""
    try:
        from deployment.dualmom_live import book as B
        from deployment.dualmom_live import data_refresh as D
        from deployment.dualmom_live import ledger as L
        from deployment.dualmom_live import month_gate as G
        today = datetime.now(IST).date()
        if G._fyers_traded_on(D._connect(), today) is False:
            _log("EOD snapshot skipped: exchange did not trade today")
            return
        client = _client()
        cap = L.capture(client)
        bk = B.live(client, use_cache=False)
        if not bk.get("positions") and not L.read_nav_daily():
            _log("EOD snapshot skipped: no positions yet")
            return
        chains = L.verify_all()
        wrote = L.record_daily(B.snapshot_row(bk, B.benchmark_close()), {
            "positions_detail": bk["rows"], "reconciliation": bk["reconciliation"],
            "capture": cap, "ledger_chains": chains,
            "charges_rate_card": C_RATE_CARD()})
        if not bk["reconciliation"]["ok"]:
            L.event("reconciliation_break", bk["reconciliation"])
            _log(f"RECONCILIATION BREAK: {bk['reconciliation']['breaks']}")
        if not all(v["ok"] for v in chains.values()):
            L.event("ledger_chain_broken", chains)
            _log(f"LEDGER CHAIN BROKEN: {chains}")
        _log(f"EOD snapshot {'written' if wrote else 'already present'}: NAV Rs {bk['nav']:,.0f} "
             f"({bk['total_return_pct']:+.2f}%), {bk['positions']} positions, "
             f"recon={'OK' if bk['reconciliation']['ok'] else 'BREAK'}")
    except Exception as e:
        _log(f"EOD snapshot FAILED: {_etext(e)}")


def C_RATE_CARD():
    from deployment.dualmom_live import config as C
    return C.CHARGES_RATE_CARD


# ── Kite (main Zerodha account) ──────────────────────────────────────────────
#
# Same strategy, second account. Every job is independent of the Kotak ones: a
# Kite failure is logged and never touches the Kotak session, and vice versa.
# Kite forgets orders overnight, so capture runs every few minutes all session.

def _kite_deployed() -> bool:
    from deployment.dualmom_kite import ledger as KL
    return bool(KL._read("fills.jsonl"))


def _job_kite_rebalance():
    """09:22 (+10:05 catch-up) - once per calendar month, after the first deploy."""
    try:
        from deployment.dualmom_kite import engine as KE
        from deployment.dualmom_kite import kite_equity as KK
        from deployment.dualmom_kite import ledger as KL
        from deployment.dualmom_live import data_refresh as D
        from deployment.dualmom_live import month_gate as G
        from deployment.dualmom_live import signal_engine as S
        if not _kite_deployed():
            return                          # the first entry is a supervised deploy
        today = datetime.now(IST).date()
        last = KE.gate_state().get("last_done_month")
        if last == G.month_key(today):
            return
        dates = {x.date() for x in S.load_prices().index}
        d = G.decide(today, dates, last, market_open=True)
        if d["action"] != "run":
            _log(f"KITE monthly rebalance {d['action'].upper()}: {d['reason']}")
            return
        is_open = G._fyers_traded_on(D._connect(), today)
        if is_open is not True:
            d = G.decide(today, dates, last, market_open=is_open)
            _log(f"KITE monthly rebalance {d['action'].upper()}: {d['reason']}")
            return
        kite = KK.client()
        # an order from an earlier attempt still OPEN is not in the own book yet -
        # re-planning now would send it a second time
        KL.capture(kite)
        still_open = [o for o in KK.our_orders(kite)
                      if str(o.get("status", "")).upper() not in KK.TERMINAL]
        if still_open:
            _log(f"KITE monthly rebalance WAIT: {len(still_open)} DualMom order(s) still open")
            return
        run = KE.build_plan(kite, as_of=d["signal_date"])
        KE.save(run, f"signal_{d['signal_date']:%Y%m%d}.json")
        run = KE.execute(run, kite)
        KE.save(run)
        KE.defer_circuit(run)
        KL.capture(kite)
        done, summary = KE.complete(run)
        KL.event("monthly_rebalance", {**summary, "signal_date": str(d["signal_date"])})
        if done:
            KE.gate_mark_done(G.month_key(today), {"signal_date": str(d["signal_date"]), **summary})
        _log(f"KITE monthly rebalance {'DONE' if done else 'NOT complete'}: {summary}")
    except Exception as e:
        _log(f"KITE monthly rebalance FAILED: {_etext(e)}")


def _job_kite_pending_circuit():
    """09:25 - buy names deferred because they were at their upper circuit."""
    try:
        from deployment.dualmom_kite import engine as KE
        if not KE.load_pending_circuit():
            return
        from deployment.dualmom_kite import kite_equity as KK
        from deployment.dualmom_live import data_refresh as D
        from deployment.dualmom_live import month_gate as G
        if G._fyers_traded_on(D._connect(), datetime.now(IST).date()) is False:
            return
        _log(f"KITE pending circuit buys: {KE.run_pending_circuit(KK.client())}")
    except Exception as e:
        _log(f"KITE pending circuit FAILED: {_etext(e)}")


def _job_kite_stop_check():
    """15:26 - the -35% stop on DualMom's own Kite positions."""
    try:
        if not _kite_deployed():
            return
        from deployment.dualmom_kite import engine as KE
        from deployment.dualmom_kite import kite_equity as KK
        from deployment.dualmom_kite import ledger as KL
        kite = KK.client()
        br = KE.stop_breaches(kite)
        if not br:
            _log("KITE stop check: none below the stop")
            return
        _log(f"KITE STOP BREACH: {', '.join(o['symbol'] for o in br)}")
        run = {"plan": {"safe": True, "blocked": [], "sells": br, "buys": []}, "log": [],
               "started": datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")}
        run = KE.execute(run, kite)
        KE.save(run, f"stop_{datetime.now(IST):%Y%m%d_%H%M%S}.json")
        KL.event("stop_run", {"names": [o["symbol"] for o in br], "halted": run.get("halted")})
        KL.capture(kite)
    except Exception as e:
        _log(f"KITE stop check FAILED: {_etext(e)}")


def _job_kite_capture():
    try:
        if not _kite_deployed() and not _market_open_now():
            return
        from deployment.dualmom_kite import kite_equity as KK
        from deployment.dualmom_kite import ledger as KL
        res = KL.capture(KK.client())
        if res.get("orders") or res.get("fills") or res.get("error"):
            _log(f"KITE ledger capture: {res}")
    except Exception as e:
        _log(f"KITE ledger capture FAILED: {_etext(e)}")


def _job_kite_intraday_nav():
    if not _market_open_now() or not _kite_deployed():
        return
    try:
        from deployment.dualmom_kite import book as KB
        from deployment.dualmom_kite import kite_equity as KK
        from deployment.dualmom_kite import ledger as KL
        bk = KB.live(KK.client(), use_cache=False)
        if bk.get("positions"):
            KL.record_intraday(KB.snapshot_row(bk))
    except Exception as e:
        _log(f"KITE intraday NAV FAILED: {_etext(e)}")


def _job_kite_eod():
    """15:42 - final capture (Kite forgets today's orders overnight) + NAV row."""
    try:
        if not _kite_deployed():
            return
        from deployment.dualmom_kite import book as KB
        from deployment.dualmom_kite import kite_equity as KK
        from deployment.dualmom_kite import ledger as KL
        from deployment.dualmom_kite import config as KC
        from deployment.dualmom_live import book as B
        from deployment.dualmom_live import data_refresh as D
        from deployment.dualmom_live import month_gate as G
        if G._fyers_traded_on(D._connect(), datetime.now(IST).date()) is False:
            return
        kite = KK.client()
        cap = KL.capture(kite)
        bk = KB.live(kite, use_cache=False)
        chains = KL.verify_all()
        wrote = KL.record_daily(KB.snapshot_row(bk, B.benchmark_close()), {
            "positions_detail": bk["rows"], "reconciliation": bk["reconciliation"],
            "capture": cap, "ledger_chains": chains, "account_margin": bk.get("account_margin"),
            "charges_rate_card": KC.CHARGES_RATE_CARD})
        if not bk["reconciliation"]["ok"]:
            KL.event("reconciliation_break", bk["reconciliation"])
            _log(f"KITE RECONCILIATION BREAK: {bk['reconciliation']['breaks']}")
        _log(f"KITE EOD {'written' if wrote else 'already present'}: NAV Rs {bk['nav']:,.0f} "
             f"({bk['total_return_pct']:+.2f}%), {bk['positions']} positions")
    except Exception as e:
        _log(f"KITE EOD snapshot FAILED: {_etext(e)}")


def _build_scheduler() -> BackgroundScheduler:
    s = BackgroundScheduler(timezone=IST)
    s.add_job(_job_heartbeat, IntervalTrigger(seconds=60, timezone=IST),
              id="dm_heartbeat", coalesce=True, max_instances=1)
    # 09:20 - first session of each month; retried on later mornings if closed.
    s.add_job(_job_monthly_rebalance, CronTrigger(
        day_of_week="mon-fri", hour=9, minute=20, timezone=IST),
        id="dm_monthly_rebalance", misfire_grace_time=7200, coalesce=True,
        max_instances=1)
    # 15:25 - the -35% stop, before the close so orders can still fill.
    s.add_job(_job_stop_check, CronTrigger(
        day_of_week="mon-fri", hour=15, minute=25, timezone=IST),
        id="dm_stop_check", misfire_grace_time=240, coalesce=True, max_instances=1)
    # 16:10 - after the close, so the day's candle is settled.
    s.add_job(_job_data_refresh, CronTrigger(
        day_of_week="mon-fri", hour=16, minute=10, timezone=IST),
        id="dm_data_refresh", misfire_grace_time=14400, coalesce=True,
        max_instances=1)
    # client ledger: capture orders+fills and a NAV point every few minutes in
    # market hours; one capture shortly after startup so nothing waits for 09:15
    from datetime import timedelta as _td
    from deployment.dualmom_live import config as _C
    every = f"*/{_C.LEDGER_CAPTURE_MIN}"
    s.add_job(_job_ledger_capture, CronTrigger(
        day_of_week="mon-fri", hour="9-15", minute=every, timezone=IST),
        id="dm_ledger_capture", misfire_grace_time=240, coalesce=True, max_instances=1)
    s.add_job(_job_ledger_capture, "date", run_date=datetime.now(IST) + _td(seconds=45),
              id="dm_ledger_capture_startup", misfire_grace_time=600)
    s.add_job(_job_intraday_nav, CronTrigger(
        day_of_week="mon-fri", hour="9-15", minute=every, timezone=IST),
        id="dm_intraday_nav", misfire_grace_time=240, coalesce=True, max_instances=1)
    # 15:40 - after the close and the 15:25 stop check
    s.add_job(_job_eod_snapshot, CronTrigger(
        day_of_week="mon-fri", hour=15, minute=40, timezone=IST),
        id="dm_eod_snapshot", misfire_grace_time=7200, coalesce=True, max_instances=1)

    # ── Kite: offset from the Kotak jobs so the two accounts never run together
    from deployment.dualmom_kite import config as _KC
    for hm, jid in ((_KC.REBALANCE_TIME, "dmk_rebalance"), (_KC.REBALANCE_CATCHUP, "dmk_rebalance_catchup")):
        s.add_job(_job_kite_rebalance, CronTrigger(
            day_of_week="mon-fri", hour=hm[0], minute=hm[1], timezone=IST),
            id=jid, misfire_grace_time=3600, coalesce=True, max_instances=1)
    s.add_job(_job_kite_pending_circuit, CronTrigger(
        day_of_week="mon-fri", hour=9, minute=25, timezone=IST),
        id="dmk_pending_circuit", misfire_grace_time=3600, coalesce=True, max_instances=1)
    s.add_job(_job_kite_stop_check, CronTrigger(
        day_of_week="mon-fri", hour=15, minute=26, timezone=IST),
        id="dmk_stop_check", misfire_grace_time=180, coalesce=True, max_instances=1)
    s.add_job(_job_kite_capture, CronTrigger(
        day_of_week="mon-fri", hour="9-15", minute="1-59/5", timezone=IST),
        id="dmk_ledger_capture", misfire_grace_time=240, coalesce=True, max_instances=1)
    s.add_job(_job_kite_intraday_nav, CronTrigger(
        day_of_week="mon-fri", hour="9-15", minute="2-59/5", timezone=IST),
        id="dmk_intraday_nav", misfire_grace_time=240, coalesce=True, max_instances=1)
    s.add_job(_job_kite_eod, CronTrigger(
        day_of_week="mon-fri", hour=15, minute=42, timezone=IST),
        id="dmk_eod_snapshot", misfire_grace_time=7200, coalesce=True, max_instances=1)
    return s


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sched
    from deployment.dualmom_live import config as C
    _log(f"starting on :{PORT}  ENABLED={C.ENABLED} DRY_RUN={C.DRY_RUN}")
    if app.state.run_scheduler:
        if _acquire_lock(PORT_DUALMOM):
            _sched = _build_scheduler()
            _sched.start()
            _log("PRIMARY - scheduler started (09:20 monthly rebalance / 15:25 stop / 16:10 data refresh / 60s heartbeat)")
        else:
            _log("another DualMom service already holds the lock — scheduler DISABLED "
                 "(this instance serves the API only)")
    else:
        _log("--no-sched — API only, no jobs")
    yield
    if _sched:
        _sched.shutdown(wait=False)
    _log("shutdown")


app = FastAPI(title="DualMom Live Service", lifespan=lifespan)
app.state.run_scheduler = True
app.include_router(live_router)
from deployment.dualmom_kite_api import router as kite_router   # /api/dualmom/live/kite/*
app.include_router(kite_router)


@app.get("/health")
async def health():
    from deployment.dualmom_live import config as C
    return {
        "ok": True,
        "service": "dualmom",
        "enabled": C.ENABLED,
        "dry_run": C.DRY_RUN,
        "scheduler": _sched is not None and _sched.running,
        "jobs": [
            {"id": j.id, "next_run": str(j.next_run_time)}
            for j in (_sched.get_jobs() if _sched else [])
        ],
        "time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sched", action="store_true",
                    help="serve the API without running the scheduled jobs")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    app.state.run_scheduler = not a.no_sched
    # ws="none": this service is pure REST. uvicorn otherwise auto-imports a
    # WebSocket backend, and the installed `websockets` has dropped the `legacy`
    # module uvicorn reaches for -> ModuleNotFoundError before the app ever starts.
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning", ws="none")
