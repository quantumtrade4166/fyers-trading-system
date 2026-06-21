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

IST = pytz.timezone("Asia/Kolkata")

# cooldown tracking: pair_name -> bars since last trade closed
_cooldown: dict[str, int] = {}
COOLDOWN_BARS = 5


def _run_signal_check():
    """Per-minute intraday job — check signals, fire paper entries/exits."""
    from deployment import signal_engine, order_router, live_feed, positions as pos_store
    from deployment.pair_config import PAIRS, NAME, SYM_A, SYM_B, QTY_A, QTY_B, ENTRY_Z, STOP_Z, ANNUAL_STOP, EXIT_Z

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
        annual_pl = pos_store.get_annual_pnl(name)
        annual_ok = annual_pl > -ann_stp

        # ── manage existing position ──────────────────────────────────────────
        if existing:
            direction   = existing["direction"]
            should_stop = abs(z) >= stop_z
            should_exit = (
                (direction == "long_spread"  and z >= -EXIT_Z) or
                (direction == "short_spread" and z <=  EXIT_Z)
            )

            if should_stop:
                order_router.execute_signal(
                    name, "stop", price_a, price_b,
                    qty_a, qty_b, z, beta, exit_reason="z_stop"
                )
                _cooldown[name] = COOLDOWN_BARS
            elif should_exit:
                order_router.execute_signal(
                    name, "exit", price_a, price_b,
                    qty_a, qty_b, z, beta, exit_reason="z_exit"
                )
                _cooldown[name] = COOLDOWN_BARS
            continue

        # ── decrement cooldown ────────────────────────────────────────────────
        if _cooldown.get(name, 0) > 0:
            _cooldown[name] -= 1
            continue

        # ── check for new entry ───────────────────────────────────────────────
        if not annual_ok:
            continue
        if not hl_ok:
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
    """15:35 EOD — reload prices from disk, run final signal check."""
    print("  [scheduler] EOD run — reloading prices...")
    from deployment import signal_engine
    signal_engine.reload_prices()
    print("  [scheduler] EOD run complete.")


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

    # start feed at 09:15
    sched.add_job(_start_feed, CronTrigger(
        day_of_week="mon-fri", hour=9, minute=15, timezone=IST))

    # per-minute signal check 09:16 to 15:29
    sched.add_job(_run_signal_check, CronTrigger(
        day_of_week="mon-fri", hour="9-15", minute="*", timezone=IST,
        start_date="2000-01-01 09:16:00", end_date="2099-01-01 15:29:00"
    ))

    # stop feed at 15:30
    sched.add_job(_stop_feed, CronTrigger(
        day_of_week="mon-fri", hour=15, minute=30, timezone=IST))

    # EOD price reload at 15:35
    sched.add_job(_eod_run, CronTrigger(
        day_of_week="mon-fri", hour=15, minute=35, timezone=IST))

    return sched
