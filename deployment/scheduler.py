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


def _strangle_intraday():
    """Every 2 min during market hours — refresh today's Vwap Strangle charts
    (near-live; strikes selected once at 9:20 and cached)."""
    import sys
    from pathlib import Path
    sp = str(Path(__file__).parent.parent / "live_trading_options" / "strangle_strategy")
    if sp not in sys.path:
        sys.path.append(sp)
    try:
        import live_capture
        live_capture.capture_all()
    except Exception as e:
        print(f"  [scheduler] strangle intraday failed: {e}")


def _start_feed():
    print("  [scheduler] Market open — starting live feed...")
    from deployment import live_feed
    live_feed.start_feed()


def _stop_feed():
    print("  [scheduler] Market close — stopping live feed...")
    from deployment import live_feed
    live_feed.stop_feed()


def create_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=IST)

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

    # Vwap Strangle intraday chart capture every 2 min during market hours
    sched.add_job(_strangle_intraday, CronTrigger(
        day_of_week="mon-fri", hour="9-15", minute="*/2", timezone=IST))

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
