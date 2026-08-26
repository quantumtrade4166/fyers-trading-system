"""test_exit_safety.py — VWAP strangle: order-book pricing and never-give-up exit.

OFFLINE. Builds a LiveController with a stub broker; places no orders. (Do not
confuse this with dry_test.py, which fires REAL orders as a live smoke test and
must only be run during market hours.)

Covers the two changes on the exit path:

  1. `_limit_price` prices off the real order book when it can be read, and falls
     back to the old mark-multiple when it cannot.
  2. Being killed while still short keeps re-flattening. That retry used to depend
     on MTM STAYING breached — if premiums eased back, the leg sat there
     unprotected until the 15:14 square-off.

    .venv\\Scripts\\python.exe live_trading_options/strangle_strategy/live/test_exit_safety.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # strangle_strategy/ (so `live.` resolves)

from live.controller import LiveController
from live.ledger import SELL, BUY

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"    ok   {name}")
    else:
        FAIL += 1
        print(f"    FAIL {name}: got {got!r}, want {want!r}")


CE = "NSE:NIFTY26AUG24500CE"
PE = "NSE:NIFTY26AUG24200PE"
SYMS = {CE: {"tradingsymbol": "NIFTY26AUG24500CE", "exchange": "NFO"},
        PE: {"tradingsymbol": "NIFTY26AUG24200PE", "exchange": "NFO"}}


class StubKite:
    """Serves a fixed book; records nothing, places nothing."""

    def __init__(self, sell_levels=None, fail=False):
        self.sell = sell_levels if sell_levels is not None else [
            {"price": 31.0, "quantity": 500}]
        self.fail = fail
        self.quote_calls = 0

    def quote(self, keys):
        self.quote_calls += 1
        if self.fail:
            raise RuntimeError("quote unavailable")
        return {keys[0]: {"last_price": 30.0,
                          "depth": {"buy": [{"price": 29.5, "quantity": 400}],
                                    "sell": self.sell}}}


def ctrl(kite=None, mode="paper"):
    c = LiveController("NIFTY", "2026-08-27", CE, PE, 0,
                       lot_size=65, lots=1, max_cycles=4, mtm_stop=16000,
                       entry_cutoff="14:30", square_off="15:14",
                       mode=mode, kite=kite, kite_syms=SYMS, allow_live=True)
    c._check_control = lambda: None            # no control file in a test
    c._write_tick = lambda combined: None      # no state files
    c.persist = lambda: None
    return c


print("\n  ── _limit_price: the book wins, the mark is the fallback ──")

c = ctrl(mode="paper")
c.marks[CE] = 30.0
# paper is not armed, so no quote should even be attempted
check("paper uses the mark multiple", round(c._limit_price(CE, BUY), 2), 39.0)

k = StubKite()
c = ctrl(kite=k, mode="live")
c.marks[CE] = 30.0
check("live prices off the book (31.00 offer + 2 ticks)",
      c._limit_price(CE, BUY, cushion_ticks=2), 31.10)
check("the book was actually consulted", k.quote_calls, 1)

# a THIN book: our 65 lots do not clear at the touch, so we must reach deeper
k = StubKite(sell_levels=[{"price": 31.0, "quantity": 25},
                          {"price": 33.0, "quantity": 25},
                          {"price": 36.0, "quantity": 900}])
c = ctrl(kite=k, mode="live")
c.marks[CE] = 30.0
depth_px = c._limit_price(CE, BUY, cushion_ticks=2)
check("thin book reaches the level that clears our size", depth_px, 36.10)
check("and stays well inside a blind 90% multiple", depth_px < 30.0 * 1.9, True)

# broker refuses the quote -> must not raise, must fall back
k = StubKite(fail=True)
c = ctrl(kite=k, mode="live")
c.marks[CE] = 30.0
check("quote failure falls back to the mark multiple",
      round(c._limit_price(CE, BUY), 2), 39.0)

c = ctrl(kite=StubKite(), mode="live")
c.marks[CE] = 0.0
check("no mark and no usable ref still returns something marketable",
      c._limit_price(CE, BUY) > 0, True)

print("\n  ── selling prices down through the bids ──")
c = ctrl(kite=StubKite(), mode="live")
c.marks[CE] = 30.0
check("sell reaches through the bid", c._limit_price(CE, SELL, cushion_ticks=2), 29.40)


print("\n  ── killed and still short: it must keep trying ──")

c = ctrl(mode="paper")
c.marks[CE] = 30.0
c.marks[PE] = 28.0

calls = []
c._flatten = lambda reason: calls.append(reason)
# pretend a leg is still short and the guard has already killed
c.ledger.open_shorts = lambda: {CE: 65}
c.guard.killed = True
c.guard.check_mtm = lambda marks: (False, -200.0)      # MTM has RECOVERED
c.guard.must_square_off = lambda now: False            # and it is not 15:14 yet

c._last_flatten_try = 0.0
c.on_tick(58.0, 30.0, 28.0, "11:00:00")
check("recovered MTM still retries the flatten", len(calls), 1)
check("and says why", "still short after kill" in (calls[0] if calls else ""), True)

# throttled: an immediate second tick must NOT fire another attempt
c.on_tick(58.0, 30.0, 28.0, "11:00:01")
check("throttled to ~2s", len(calls), 1)

# once genuinely flat, nothing more is attempted
c.ledger.open_shorts = lambda: {}
c._last_flatten_try = 0.0
c.on_tick(58.0, 30.0, 28.0, "11:00:05")
check("flat -> no further attempts", len(calls), 1)

# and a live MTM breach still takes priority with its own reason
c2 = ctrl(mode="paper")
c2.marks[CE], c2.marks[PE] = 30.0, 28.0
calls2 = []
c2._flatten = lambda reason: calls2.append(reason)
c2.ledger.open_shorts = lambda: {CE: 65}
c2.guard.killed = True
c2.guard.check_mtm = lambda marks: (True, -20000.0)
c2.on_tick(58.0, 30.0, 28.0, "11:00:00")
check("an active breach reports as the MTM stop", calls2, ["MTM stop"])

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
