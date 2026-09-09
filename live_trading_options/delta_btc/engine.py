"""
engine.py — runs all three session profiles side by side on one chain feed.
===========================================================================

One process, one poll loop, three books. A, B and C see the IDENTICAL chain
snapshot at the IDENTICAL instant, so any difference between their results after
a month is attributable to the session rule and not to one of them having had a
luckier view of the market. That is the whole reason they run in one engine
instead of three.

    A  ist_day      09:30 -> 17:10 IST, same-day expiry
    B  full_cycle   17:35 -> 17:10 next day, one strangle per expiry
    C  continuous   17:35 -> 17:10 next day, re-enters after a stop-out

PAPER ONLY. No API key is required and no order can reach an exchange: the
executor's live path raises rather than being merely disabled.

EXPIRY HANDLING
At most two expiries are ever in play. From 09:30 to 17:10 every profile wants
today's; between 17:35 and the next settlement B and C want tomorrow's while A is
out of session. Chains are built lazily per expiry code and cached. A controller
that is HOLDING a position keeps the chain it sold into, latched at entry, so a
roll can never re-price an open leg against a contract it does not own.

Run:
    .venv/Scripts/python.exe live_trading_options/delta_btc/engine.py

Start it any time — each profile picks up at its next entry. It is safe to
restart: paper books rebuild from the next cycle, and the completed-cycle log in
data/results/cycles.jsonl is append-only, so nothing already recorded is lost.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
import time
import traceback
import datetime as dt

from core.shared import singleton, PORT_BTC_ENGINE, now_ist, STATE_DIR, LOGS
from core.api import btc_options, btc_option_tickers, DeltaError
from core.chain import LiveChain, nearest_expiry
from core.sessions import load_profiles
from live.controller import BTCController

PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text(encoding="utf-8"))
POLL = int(PARAMS.get("poll_seconds", 5))
STALL = int(PARAMS.get("stall_seconds", 180))
SPAN = int(PARAMS.get("chain_span", 20))
SETTLE = PARAMS.get("settlement_time_ist", "17:30")

_chains: dict = {}          # expiry code -> LiveChain
_products: list = []        # the product master, refreshed daily
_products_day = None
_log_path = LOGS / "engine.log"


def log(msg: str):
    line = f"{now_ist().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def products(force: bool = False) -> list:
    """The BTC product master, refreshed once a day.

    New daily expiries list at 17:30, so a master cached at engine start would go
    stale within hours and the next cycle would find no contracts. Keyed on the
    IST date, plus a forced refresh whenever a wanted expiry is missing.
    """
    global _products, _products_day
    today = now_ist().date()
    if force or not _products or _products_day != today:
        _products = btc_options()
        _products_day = today
        log(f"  product master refreshed: {len(_products)} live BTC contracts")
    return _products


def chain_for(expiry: str):
    """The LiveChain for one expiry, built on first use."""
    if expiry not in _chains:
        rows = products()
        try:
            _chains[expiry] = LiveChain(expiry, span=SPAN, settle_ist=SETTLE).load(rows)
        except RuntimeError:
            # the expiry listed after our cached master was taken — refresh once
            _chains[expiry] = LiveChain(expiry, span=SPAN,
                                        settle_ist=SETTLE).load(products(force=True))
        log(f"  chain built for {expiry}: {len(_chains[expiry].by_key)} contracts, "
            f"{len(_chains[expiry].strikes)} strikes")
    return _chains[expiry]


def drop_settled(now: dt.datetime):
    """Forget chains whose expiry has settled, so the cache cannot grow all month
    and a stale expiry can never be handed to a controller."""
    for code in list(_chains):
        if _chains[code].seconds_to_settlement(now) < -300:
            _chains.pop(code, None)
            log(f"  chain {code} settled — dropped")


def validate(profiles: dict) -> dict:
    """Refuse to run a profile whose max-loss sits below its own worst case.

    If the limit is under what the two leg stops themselves can lose, it fires
    first and the per-leg stops never get to work — the position is closed by the
    wrong mechanism and the strategy under test is not the strategy that ran. That
    is a config error worth failing loudly on, not a warning to scroll past.
    """
    ok = {}
    for name, p in profiles.items():
        limit = abs(float((PARAMS.get("max_loss_usd") or {}).get(name, 0)))
        worst = p.worst_case_both_stopped()
        if limit <= worst:
            log(f"  !! {name} DISABLED: max_loss ${limit} <= worst case ${worst}. "
                f"The loss limit would fire before the leg stops could work.")
            continue
        ok[name] = p
    return ok


def main():
    if not singleton.acquire(PORT_BTC_ENGINE):
        log("another engine holds the lock — this duplicate exits.")
        return

    profiles = validate(load_profiles(PARAMS))
    if not profiles:
        log("no runnable profiles — aborting.")
        return

    log(f"engine start — PAPER ONLY, poll {POLL}s")
    for p in profiles.values():
        log(f"  {p.describe()}")

    ctrls = {name: BTCController(p, PARAMS) for name, p in profiles.items()}
    # Pick up any cycle that was running when this engine last stopped, BEFORE the
    # first poll reaches a controller — otherwise the next window sees a flat book
    # and opens a second strangle on top of the one it was already holding.
    start = now_ist()
    for name, c in ctrls.items():
        try:
            if c.restore(start):
                log(f"  {name}: resumed cycle {c.cycle} — {c.position.n_live} leg(s) live, "
                    f"realized ${c.position.realized():+.2f}")
        except Exception as e:
            log(f"  {name}: resume failed ({e}) — starting this cycle fresh")
    # The push feed is an ACCELERATOR, never a dependency. It gives sub-second
    # stop detection and a live ticker; with it down the engine keeps working off
    # the REST chain exactly as before, just noticing a stop up to POLL seconds
    # later. Nothing below is allowed to raise if the socket never connects.
    feed = None
    try:
        from live.ws_feed import WSFeed
        feed = WSFeed(on_tick=lambda: write_tick(ctrls, feed)).start()
        for c in ctrls.values():
            c.feed = feed
        log("  push feed started (spot + held legs)")
    except Exception as e:
        log(f"  push feed unavailable ({type(e).__name__}: {e}) — REST only")

    last_good = time.monotonic()
    polls = fails = 0

    while True:
        started = time.monotonic()
        now = now_ist()
        try:
            tickers = btc_option_tickers()
            front = nearest_expiry(now, products(), SETTLE)
            if front is None:
                products(force=True)
                front = nearest_expiry(now, products(), SETTLE)
            if front is None:
                raise DeltaError("no unsettled BTC expiry found")

            # refresh only the chains actually in use this poll
            wanted = {front}
            for c in ctrls.values():
                if c.expiry and not c.position.is_flat:
                    wanted.add(c.expiry)
            for code in wanted:
                chain_for(code).refresh(tickers)

            for name, c in ctrls.items():
                # a controller holding a position keeps the expiry it sold into
                code = c.expiry if (c.expiry and not c.position.is_flat) else front
                try:
                    c.on_tick(chain_for(code), now)
                except Exception as e:
                    # never let one profile's failure stop the others — they may be
                    # holding positions that still need their square-off
                    log(f"  !! {name} step error: {type(e).__name__}: {e}")
                    log(traceback.format_exc(limit=6))

            # follow exactly the legs that are live right now — cheap when
            # unchanged, which is every poll except an entry, adjust or stop-out
            if feed is not None:
                feed.track([leg.symbol for c in ctrls.values()
                            for leg in (c.position.ce, c.position.pe)
                            if leg is not None and leg.is_live])

            drop_settled(now)
            last_good = time.monotonic()
            polls += 1
            if polls % 60 == 0:
                ch = chain_for(front)
                bits = " | ".join(
                    f"{n}:{'IN' if c.cycle else '--'} "
                    f"{c.position.n_live}L mtm${c.mtm:+.2f}"
                    for n, c in ctrls.items())
                log(f"  poll {polls}  spot {ch.spot}  atm {ch.atm}  exp {front}  {bits}")
            write_combined(ctrls, front, feed)
        except Exception as e:
            fails += 1
            log(f"  poll failed ({fails}): {type(e).__name__}: {e}")

        if (time.monotonic() - last_good) > STALL:
            log(f"  no good poll for {STALL}s — exiting for a clean restart")
            os._exit(1)

        time.sleep(max(0.0, POLL - (time.monotonic() - started)))


def write_combined(ctrls: dict, front: str, feed=None):
    """The single file the dashboard tab reads.

    Carries the whole picture in one request: every profile's book, the option
    chain around ATM with our own sold strikes flagged, and the live feed's
    health. One file rather than five endpoints, because the tab refreshes on a
    timer and five round trips per refresh is how a dashboard gets slow.
    """
    try:
        snaps = {n: c.snapshot() for n, c in ctrls.items()}
        chain = chain_for(front)
        # which strikes WE are short, so the chain table can mark them
        sold = {}
        for n, c in ctrls.items():
            for leg in (c.position.ce, c.position.pe):
                if leg is not None and leg.is_live:
                    sold.setdefault(f"{leg.strike:.0f}{leg.opt_type}", []).append(n)
        (STATE_DIR / "ALL_STATE.json").write_text(json.dumps({
            "updated": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "front_expiry": front, "paper": True,
            "usd_inr": PARAMS.get("usd_inr"),
            "spot": chain.spot, "atm": chain.atm,
            "seconds_to_settlement": chain.seconds_to_settlement(now_ist()),
            "chain": chain.rows(span=10),
            "sold": sold,
            "feed": feed.status() if feed else {"live": False},
            "profiles": snaps,
            "totals": {n: s["total"] for n, s in snaps.items()},
        }, indent=1, default=str), encoding="utf-8")
    except Exception:
        pass


def write_tick(ctrls: dict, feed):
    """A TINY file, rewritten on every push tick.

    Separate from ALL_STATE on purpose. The tab polls this at ~1s for the ticker
    and the live P&L, and ALL_STATE far less often for the chain and the tables.
    Rewriting the big file at tick rate would be pointless I/O; making the tab
    wait for the big file to see a price move is what makes a screen feel dead.
    """
    try:
        legs = []
        for n, c in ctrls.items():
            for leg in (c.position.ce, c.position.pe):
                if leg is None or not leg.is_live:
                    continue
                q = feed.quote(leg.symbol) or {}
                legs.append({"profile": n, "side": leg.opt_type, "strike": leg.strike,
                             "symbol": leg.symbol, "entry": leg.entry_price,
                             "mark": q.get("mark"), "bid": q.get("bid"),
                             "ask": q.get("ask"), "sl": leg.sl_trigger})
        (STATE_DIR / "TICK.json").write_text(json.dumps({
            "ts": now_ist().strftime("%H:%M:%S"),
            "spot": feed.spot(),
            "live": feed.status().get("live"),
            "mtm": {n: c.mtm for n, c in ctrls.items()},
            "legs": legs,
        }, default=str), encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted — exiting.")
