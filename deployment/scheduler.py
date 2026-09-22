"""
scheduler.py
APScheduler jobs:
  09:15 IST        — start live WebSocket feed
  09:16-15:29 IST  — per-minute intraday signal check (entries/exits/stops)
  15:30 IST        — stop WebSocket feed
  15:35 IST        — EOD run: reload prices from disk
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import pytz
import numpy as np
from datetime import date

IST = pytz.timezone("Asia/Kolkata")

# cooldown tracking: pair_name -> exit date (calendar date, not bar counter)
# Bug 2 fix: cooldown must be 5 TRADING DAYS, not 5 signal-check bars (5 bars = 25 min intraday)
_cooldown_exit_date: dict[str, date] = {}
COOLDOWN_DAYS = 5


def _trading_days_since(exit_dt: date) -> int:
    """Count trading days (business days) between exit_dt and today."""
    today = date.today()
    if today <= exit_dt:
        return 0
    return int(np.busday_count(exit_dt.isoformat(), today.isoformat()))


def _run_signal_check():
    """Per-minute intraday job — check signals, fire paper entries/exits."""
    from deployment import signal_engine, order_router, live_feed, positions as pos_store
    from deployment.pair_config import PAIRS, NAME, SYM_A, SYM_B, QTY_A, QTY_B, LOTS_A, LOTS_B, ENTRY_Z, STOP_Z, ANNUAL_STOP, EXIT_Z

    live_prices = live_feed.get_live_prices() if live_feed.is_running() else {}
    signals = signal_engine.get_all_signals(today_prices=live_prices or None)

    for p in PAIRS:
        name    = p[NAME]
        stats   = signals.get(name, {})
        z       = stats.get("z")
        if z is None or stats.get("error"):
            continue

        price_a = stats.get("price_a", 0)
        price_b = stats.get("price_b", 0)
        qty_a   = p[QTY_A]
        qty_b   = p[QTY_B]
        entry_z = p[ENTRY_Z]
        stop_z  = p[STOP_Z]
        ann_stp = p[ANNUAL_STOP]
        beta    = stats.get("beta", 0)
        hl_ok   = stats.get("hl_ok", False)

        existing  = pos_store.get_position(name)

        # ── Bug 3 fix: annual P&L includes open position MTM for year-boundary safety ──
        annual_pl = pos_store.get_annual_pnl(name)
        if existing:
            # add unrealised MTM so annual stop accounts for open loss
            sign  = 1 if existing["direction"] == "long_spread" else -1
            mtm   = ((price_a - existing["entry_price_a"]) * existing["qty_a"]
                     - (price_b - existing["entry_price_b"]) * existing["qty_b"]) * sign
            annual_pl_with_mtm = annual_pl + mtm
        else:
            annual_pl_with_mtm = annual_pl
        annual_ok = annual_pl_with_mtm > -ann_stp

        # ── manage existing position ──────────────────────────────────────────
        if existing:
            direction   = existing["direction"]
            should_stop = abs(z) >= stop_z or not annual_ok
            should_exit = (
                (direction == "long_spread"  and z >= -EXIT_Z) or
                (direction == "short_spread" and z <=  EXIT_Z)
            )

            if should_stop:
                reason = "annual_stop" if not annual_ok else "z_stop"
                order_router.execute_signal(
                    name, "stop", price_a, price_b,
                    qty_a, qty_b, z, beta, exit_reason=reason
                )
                _cooldown_exit_date[name] = date.today()
            elif should_exit:
                order_router.execute_signal(
                    name, "exit", price_a, price_b,
                    qty_a, qty_b, z, beta, exit_reason="z_exit"
                )
                _cooldown_exit_date[name] = date.today()
            continue

        # ── Bug 2 fix: cooldown in trading days, not signal-check bars ────────
        exit_dt = _cooldown_exit_date.get(name)
        if exit_dt is not None and _trading_days_since(exit_dt) < COOLDOWN_DAYS:
            continue

        # ── check for new entry ───────────────────────────────────────────────
        if not annual_ok:
            continue
        if not hl_ok:
            continue

        # ── Bug 1 fix: never enter if z already at or beyond stop level ───────
        if abs(z) >= stop_z:
            continue

        if z < -entry_z:
            order_router.execute_signal(
                name, "long_spread", price_a, price_b,
                qty_a, qty_b, z, beta
            )
        elif z > entry_z:
            order_router.execute_signal(
                name, "short_spread", price_a, price_b,
                qty_a, qty_b, z, beta
            )


def _eod_run():
    """15:35 EOD — update daily parquet data, reload prices, refresh DualMom signal."""
    print("  [scheduler] EOD run — updating daily data...")
    from deployment import signal_engine, dualmom_engine
    from deployment.update_pair_data import update_symbols
    try:
        update_symbols()
    except Exception as e:
        print(f"  [scheduler] daily data update failed: {e}")
    signal_engine.reload_prices()
    dualmom_engine.refresh()
    print("  [scheduler] EOD run complete.")


def _dualmom_eod():
    """16:00 EOD — record DualMom paper NAV, run month-end rebalance if needed."""
    print("  [scheduler] DualMom EOD run...")
    from deployment import dualmom_paper
    dualmom_paper.record_daily_nav()
    if dualmom_paper.is_last_trading_day():
        print("  [scheduler] Last trading day of month — running rebalance...")
        dualmom_paper.run_month_end_rebalance()
    print("  [scheduler] DualMom EOD run complete.")


def _zerodha_login():
    """08:50 IST — generate today's Kite Connect access token (headless TOTP)."""
    print("  [scheduler] Zerodha auto-login...")
    from deployment.brokers import zerodha_auto_login
    if zerodha_auto_login.ensure_token():
        print("  [scheduler] Zerodha token ready.")
    else:
        print("  [scheduler] Zerodha token NOT generated (check creds / TOTP).")


def _xts_eod_snapshot():
    """15:30 IST — record XTS end-of-day P&L (resets daily, so we persist it)."""
    print("  [scheduler] Recording XTS EOD P&L...")
    from deployment import broker_eod
    try:
        broker_eod.record_eod()
    except Exception as e:
        print(f"  [scheduler] XTS EOD record failed: {e}")


def _run_with_timeout(fn, seconds, name):
    """Run fn() in a daemon thread; if it exceeds `seconds`, abandon it and return
    False (the leaked daemon thread cannot block the next scheduler cycle). This is
    the guard against a single hung Fyers/network call permanently freezing the
    2-min capture — the failure mode that killed capture today."""
    import threading
    done = threading.Event()
    err = {}
    def _target():
        try:
            fn()
        except Exception as e:
            err["e"] = e
        finally:
            done.set()
    threading.Thread(target=_target, daemon=True, name=name).start()
    if not done.wait(seconds):
        print(f"  [scheduler] {name} exceeded {seconds}s — abandoned (retry next cycle)")
        return False
    if "e" in err:
        print(f"  [scheduler] {name} error: {err['e']}")
        return False
    return True


def _strangle_intraday():
    """Every 2 min during market hours — refresh today's Vwap Strangle charts and
    keep the V2 engine alive. Hardened: capture runs under a HARD TIMEOUT so a hung
    Fyers call can't freeze all future cycles, and V2 supervision can't raise out."""
    import sys
    from pathlib import Path
    sp = str(Path(__file__).parent.parent / "live_trading_options" / "strangle_strategy")
    if sp not in sys.path:
        sys.path.append(sp)
    try:
        import live_capture
        _run_with_timeout(live_capture.capture_all, 60, "strangle-capture")
    except Exception as e:
        print(f"  [scheduler] strangle intraday failed: {e}")
    # start V2 the moment strikes are cached / restart it if it stalls — never raise
    try:
        _ensure_v2_running()
    except Exception as e:
        print(f"  [scheduler] ensure_v2 failed: {e}")
    # the delta-neutral engine is independent of V2 — one failing must never stop
    # the other, so it gets its own try
    try:
        _ensure_dn_running()
    except Exception as e:
        print(f"  [scheduler] ensure_dn failed: {e}")
    # Nifty Directional Pivot (paper) — independent of both engines above
    try:
        _ensure_ndp_running()
    except Exception as e:
        print(f"  [scheduler] ensure_ndp failed: {e}")


_V2_PS_FILTER = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' "
                 "-and $_.CommandLine -like '*tick_engine*' }")


def _ensure_v2_running():
    """Start the V2 tick engine the MOMENT the day's strikes are cached (right
    after the 9:20 selection) — not on a fixed clock — and keep it alive.
    Called from the 2-min intraday job. Actions only when strikes exist:
      - not running        -> start it
      - running but STALLED -> (silent WS stall: candles frozen while process
                               keeps writing) kill the zombie + restart
      - running and fresh   -> do nothing"""
    import datetime, subprocess, json as _json
    from pathlib import Path
    import pytz
    now = datetime.datetime.now(pytz.timezone("Asia/Kolkata"))
    if not (now.weekday() < 5 and (9, 20) <= (now.hour, now.minute) <= (15, 28)):
        return
    root = Path(__file__).parent.parent / "live_trading_options" / "strangle_strategy"
    today = now.strftime("%Y-%m-%d")
    # V2 can't run before the strikes are chosen — wait for the selection cache
    state = root / "data" / "intraday_state"
    if not any((state / f"{today}_{i}.json").exists() for i in ("NIFTY", "SENSEX")):
        return
    # is the engine process actually running?
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"({_V2_PS_FILTER} | Measure-Object).Count"],
                        capture_output=True, text=True, timeout=20)
    running = (r.stdout.strip() or "0") != "0"
    # Liveness is judged from the TICK file mtime the engine rewrites every ~0.4s
    # (data/live_state/{today}_{i}_TICK.json) — NOT the chart-archive candle time.
    # The chart archive only advances on a live 5-min close, so for an engine
    # RESTARTED late in the day it stays frozen (backfill fills only the early gap,
    # never candles up to `now`). The old candle-time check then saw freshest>480
    # forever and Stop-Process'd a perfectly healthy, ticking engine every 2-min
    # cycle -> permanent churn. The tick mtime is fresh whenever the engine lives,
    # exactly like _ensure_dn_running.  (2026-09-22 incident.)
    live = root / "data" / "live_state"
    gap = None
    for i in ("NIFTY", "SENSEX"):
        f = live / f"{today}_{i}_TICK.json"
        if not f.exists():
            continue
        g = datetime.datetime.now().timestamp() - f.stat().st_mtime
        gap = g if gap is None else min(gap, g)
    # grace until 9:30 so a just-started engine isn't judged before its first tick
    stalled = running and now.time() >= datetime.time(9, 30) and (gap is None or gap > 180)
    if (not running) or stalled:
        if stalled:      # kill the hung/stale engine before starting fresh
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"{_V2_PS_FILTER} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
                           capture_output=True, timeout=20)
        subprocess.run(["schtasks", "/Run", "/TN", "StrangleV2Engine"], capture_output=True, timeout=20)
        print(f"  [scheduler] V2 {'restart' if stalled else 'start'} "
              f"(running={running}, tick_gap={gap})")


_DN_PS_FILTER = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' "
                 "-and $_.CommandLine -like '*delta_neutral*engine*' }")


def _ensure_dn_running():
    """Keep the delta-neutral engine alive from 09:20.

    It must be up BEFORE its 09:30 entry, so unlike the V2 engine (which only
    starts once strikes are cached) this one starts on the clock. Staleness is
    judged from the TICK file the controller rewrites every ~0.4s: if that stops
    advancing the engine is a zombie and gets replaced.
    """
    import datetime, subprocess, json as _json
    from pathlib import Path
    import pytz
    now = datetime.datetime.now(pytz.timezone("Asia/Kolkata"))
    if not (now.weekday() < 5 and (9, 20) <= (now.hour, now.minute) <= (15, 20)):
        return

    state = (Path(__file__).parent.parent / "live_trading_options" / "delta_neutral"
             / "data" / "live_state")
    today = now.strftime("%Y-%m-%d")

    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"({_DN_PS_FILTER} | Measure-Object).Count"],
                       capture_output=True, text=True, timeout=20)
    running = (r.stdout.strip() or "0") != "0"

    # how long since the newest TICK file was last written (seconds)
    gap = None
    for i in ("NIFTY", "SENSEX"):
        f = state / f"{today}_{i}_TICK.json"
        if not f.exists():
            continue
        g = (datetime.datetime.now().timestamp() - f.stat().st_mtime)
        gap = g if gap is None else min(gap, g)
    # grace until 9:25 so a just-started engine isn't judged before its first write
    stalled = running and now.time() >= datetime.time(9, 25) and (gap is None or gap > 180)

    if (not running) or stalled:
        if stalled:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"{_DN_PS_FILTER} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
                           capture_output=True, timeout=20)
        subprocess.run(["schtasks", "/Run", "/TN", "DeltaNeutralEngine"],
                       capture_output=True, timeout=20)
        print(f"  [scheduler] DN {'restart' if stalled else 'start'} "
              f"(running={running}, tick_gap={gap})")


_NDP_PS_FILTER = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' "
                  "-and $_.CommandLine -like '*nifty_pivot*engine*' }")


def _ensure_ndp_running():
    """Keep the Nifty Directional Pivot paper engine alive 09:10-15:20 IST.

    Its first decision is the 09:20 bar, so it must be up and seeded before then
    (the scheduled task starts it at 09:10). Staleness is judged from TICK.json,
    which the engine rewrites every second from its writer thread whether or not
    the market is ticking — so a quiet market or a holiday never looks dead, but a
    hung process does.
    """
    import datetime, subprocess
    from pathlib import Path
    import pytz
    now = datetime.datetime.now(pytz.timezone("Asia/Kolkata"))
    if not (now.weekday() < 5 and (9, 10) <= (now.hour, now.minute) <= (15, 20)):
        return

    tick = (Path(__file__).parent.parent / "live_trading_options" / "nifty_pivot"
            / "data" / "live_state" / "TICK.json")

    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"({_NDP_PS_FILTER} | Measure-Object).Count"],
                       capture_output=True, text=True, timeout=20)
    running = (r.stdout.strip() or "0") != "0"

    gap = (datetime.datetime.now().timestamp() - tick.stat().st_mtime) if tick.exists() else None
    # grace until 09:14 so a just-started engine can finish seeding before its first write
    stalled = running and now.time() >= datetime.time(9, 14) and (gap is None or gap > 120)

    if (not running) or stalled:
        if stalled:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"{_NDP_PS_FILTER} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
                           capture_output=True, timeout=20)
        subprocess.run(["schtasks", "/Run", "/TN", "NiftyPivotEngine"],
                       capture_output=True, timeout=20)
        print(f"  [scheduler] NDP {'restart' if stalled else 'start'} (running={running}, tick_gap={gap})")


def _start_feed():
    print("  [scheduler] Market open — starting live feed...")
    from deployment import live_feed
    live_feed.start_feed()


def _stop_feed():
    print("  [scheduler] Market close — stopping live feed...")
    from deployment import live_feed
    live_feed.stop_feed()


_hb = {"wall": None, "mono": None}


def _heartbeat():
    """Every 60s. Keeps the scheduler from going blind after a VM pause.

    APScheduler sleeps on a MONOTONIC timer until its next job, and that timer does
    not advance while the host has the VM frozen. On 14-Sep-2026 the host paused
    the VPS 01:22-09:19 IST; afterwards this scheduler slept on until 16:47 and then
    logged EVERY job as "missed" - 08:50 Zerodha login, 09:15 feed, the per-minute
    signal check and the 2-minute strangle capture/V2 supervisor. (It was an NSE
    holiday, so nothing was lost that day. On a trading day it would have been.)

    A job every 60s forces the loop to wake within a minute of resuming and
    re-check the wall clock. It also logs the pause itself: wall time jumping far
    ahead of monotonic time is the signature of a frozen VM.

    Limit: a job whose time falls INSIDE the pause (e.g. 08:50 login during an
    overnight freeze) is still missed - this only stops the blindness AFTER resume.
    """
    import time as _t
    wall, mono = _t.time(), _t.monotonic()
    if _hb["wall"] is not None:
        gap = (wall - _hb["wall"]) - (mono - _hb["mono"])
        if gap > 120:
            print(f"  [scheduler] VM PAUSE DETECTED: wall clock jumped {gap/60:.1f} min "
                  f"more than this process ran - the host froze the machine", flush=True)
    _hb["wall"], _hb["mono"] = wall, mono


def create_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=IST)

    # 60s heartbeat - see _heartbeat(). Must stay first and must stay cheap.
    sched.add_job(_heartbeat, IntervalTrigger(seconds=60, timezone=IST),
                  id="heartbeat", coalesce=True, max_instances=1)
    print("  [scheduler] 60s heartbeat armed (VM-pause guard)", flush=True)

    # Zerodha headless auto-login at 08:50 (token ready before market open)
    sched.add_job(_zerodha_login, CronTrigger(
        day_of_week="mon-fri", hour=8, minute=50, timezone=IST))

    # start feed at 09:15
    sched.add_job(_start_feed, CronTrigger(
        day_of_week="mon-fri", hour=9, minute=15, timezone=IST))

    # per-minute signal check 09:16 to 15:29
    sched.add_job(_run_signal_check, CronTrigger(
        day_of_week="mon-fri", hour="9-15", minute="*", timezone=IST,
        start_date="2000-01-01 09:16:00", end_date="2099-01-01 15:29:00"
    ))

    # Vwap Strangle intraday chart capture every 2 min during market hours.
    # This job also starts V2 the moment strikes are cached and restarts it if it
    # stalls/dies (see _ensure_v2_running, called inside _strangle_intraday).
    sched.add_job(_strangle_intraday, CronTrigger(
        day_of_week="mon-fri", hour="9-15", minute="*/2", timezone=IST),
        max_instances=3, misfire_grace_time=90, coalesce=True)

    # stop feed at 15:30
    sched.add_job(_stop_feed, CronTrigger(
        day_of_week="mon-fri", hour=15, minute=30, timezone=IST))

    # record XTS EOD P&L at 15:30 (XTS resets daily — persist the day's number)
    sched.add_job(_xts_eod_snapshot, CronTrigger(
        day_of_week="mon-fri", hour=15, minute=30, timezone=IST))

    # EOD price reload at 15:35
    sched.add_job(_eod_run, CronTrigger(
        day_of_week="mon-fri", hour=15, minute=35, timezone=IST))

    # DualMom paper NAV + month-end rebalance at 16:00
    sched.add_job(_dualmom_eod, CronTrigger(
        day_of_week="mon-fri", hour=16, minute=0, timezone=IST))

    return sched
