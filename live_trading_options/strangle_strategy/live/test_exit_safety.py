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


print("\n  ── entry failure (margin OR reject) ALWAYS fail-safe closes via the broker ──")
# The 2026-10-05 incident: a 22800 CE SELL FILLED, the PE leg was rejected on margin,
# and the stale local ledger showed the CE "pending" so _flatten covered nothing — a
# naked CE sat all day. Now EVERY entry failure routes through _fail_safe_flatten, which
# verifies against the broker and closes anything really short.
MARGIN = ("GeneralException: Insufficient funds. Margin required: 3565383.52. "
          "Margin available: 3564721.64. Add 661.88 to place this order.")


def _raises(msg):
    def _f(*a, **k):
        raise Exception(msg)
    return _f


c = ctrl(kite=StubKite(), mode="live")
c.marks[CE], c.marks[PE] = 30.0, 28.0
fs = []
c._fail_safe_flatten = lambda reason: fs.append(reason)
c.guard.validate_entry = lambda *a, **k: (True, "")
c._sell_pair_live = _raises(MARGIN)
c._enter(58.0, 1, "test")

check("margin flagged on the event", c.events[-1].get("margin"), True)
check("margin_halt recorded", bool(c.margin_halt), True)
check("the broker's own words are kept", "661.88" in (c.margin_halt or ""), True)
check("margin -> fail-safe flatten", len(fs), 1)
check("and it says why", "margin shortfall" in (fs[0] if fs else ""), True)
check("guard killed", c.guard.killed, True)
check("snapshot carries the alarm", bool(c.snapshot().get("margin_halt")), True)

# a NON-margin rejection now ALSO fail-safe closes (a filled leg must never be abandoned)
c2 = ctrl(kite=StubKite(), mode="live")
c2.marks[CE], c2.marks[PE] = 30.0, 28.0
fs2 = []
c2._fail_safe_flatten = lambda reason: fs2.append(reason)
c2.guard.validate_entry = lambda *a, **k: (True, "")
c2._sell_pair_live = _raises("InputException: bad price")
c2._enter(58.0, 1, "test")
check("non-margin error ALSO fail-safe closes", len(fs2), 1)
check("but still kills for the day", c2.guard.killed, True)
check("and is not mislabelled as margin", c2.margin_halt, None)


print("\n  ── fail-safe VERIFIES with the broker and closes a leg the ledger lost ──")
# Reproduce 2026-10-05 exactly: the BROKER holds a real 520 CE short, but the local
# ledger is empty (the margin abort never booked the fill). _fail_safe_flatten must
# learn the short from the broker, buy it back, and CONFIRM flat against the broker.
from live import kite_executor as _kxmod

CE_TS = SYMS[CE]["tradingsymbol"]
_saved = {k: getattr(_kxmod, k) for k in ("strategy_fills", "place_limit_verified", "order_status", "cancel")}
broker = [{"tradingsymbol": CE_TS, "side": SELL, "qty": 520,
           "avg_price": 23.25, "order_id": "SELL1", "fill_time": "09:43"}]
covers = []
_kxmod.strategy_fills = lambda kite, tag="vwstrangle": list(broker)


def _fs_place(kite, tsym, exch, side, qty, price, product, tag="vwstrangle", retries=2):
    oid = f"COV{len(covers) + 1}"
    covers.append(oid)
    broker.append({"tradingsymbol": tsym, "side": BUY, "qty": qty,      # cover fills at the broker
                   "avg_price": 10.3, "order_id": oid, "fill_time": "09:44"})
    return oid


_kxmod.place_limit_verified = _fs_place
_kxmod.order_status = lambda kite, oid: {"status": "COMPLETE", "filled_qty": 520, "avg_price": 10.3, "fill_time": "09:44"}
_kxmod.cancel = lambda kite, oid: None

c3 = ctrl(kite=StubKite(), mode="live")
c3._CLOSE_POLL_S = 0.4
c3._limit_price = lambda sym, side, buf=None, cushion_ticks=2: 10.0
c3._product_for = lambda sym, side: "NRML"
check("broker shows the naked CE, ledger does not", (c3._broker_open_shorts(), c3.ledger.open_short_real(CE)), ({CE: 520}, 0))
c3._fail_safe_flatten("margin shortfall — test")
check("a cover BUY was placed", len(covers) >= 1, True)
check("broker CONFIRMS flat after fail-safe", c3._broker_open_shorts(), {})
check("ledger agrees flat", c3.ledger.open_short_real(CE), 0)
for _k, _v in _saved.items():
    setattr(_kxmod, _k, _v)



print("\n  -- product: an exit follows the POSITION, not the config --")
# Zerodha keeps MIS and NRML in separate buckets. A BUY in the wrong product does
# not close the short - it opens a fresh long beside it and leaves the short
# running. So changing the config under a live position must never orphan it.
c = ctrl(kite=StubKite(), mode="live")
c.product = "NRML"
check("a SELL uses the configured product", c._product_for(CE, SELL), "NRML")
check("the BUY that closes it reuses NRML", c._product_for(CE, BUY), "NRML")

c.product = "MIS"                       # operator changes config mid-day
check("config flip does NOT change the open position's exit",
      c._product_for(CE, BUY), "NRML")
check("but a NEW short takes the new product", c._product_for(PE, SELL), "MIS")
check("and that one exits as MIS", c._product_for(PE, BUY), "MIS")
check("the first leg is still remembered as NRML", c._product_for(CE, BUY), "NRML")

c2 = ctrl(kite=StubKite(), mode="live")
c2.product = "NRML"
check("a symbol this process never opened falls back to config",
      c2._product_for("NSE:UNSEEN", BUY), "NRML")


print("\n  -- a close NEVER gives up: re-prices off depth until the short is flat --")
# The 2026-09-30 SENSEX incident: an exit fired on a candle closing above VWAP, the
# PE bought back, but the rising CE's buy-back missed the fill and the leg sat naked
# until the user hit Kill. A close must keep lifting deeper into the book instead.
from live import kite_executor as kx

# (a) fills on the first attempt -> one placement, no cancel
c = ctrl(kite=StubKite(), mode="live")
c.marks[CE] = 30.0
c._CLOSE_POLL_S = 0.4
c._limit_price = lambda sym, side, buf=None, cushion_ticks=2: 31.0 + (cushion_ticks or 0) * 0.05
c._product_for = lambda sym, side: "NRML"
short = {CE: 65}
c.ledger.open_short_real = lambda sym: short.get(sym, 0)
c.ledger.record = lambda o: None
c.ledger.update_fill = lambda *a, **k: None
placed, cancels = [], []
kx.place_limit_verified = lambda *a, **k: (placed.append(1) or f"oid-{len(placed)}")
kx.cancel = lambda kite, oid: cancels.append(oid)

def _status_fill_now(kite, oid):
    short[CE] = 0                                   # the fill flattens the short
    return {"status": "COMPLETE", "filled_qty": 65, "avg_price": 32.0, "fill_time": "11:00:05"}

kx.order_status = _status_fill_now
fill = c._close_leg(CE, 1, "exit", 65)
check("filled on attempt 1", (len(placed), len(cancels)), (1, 0))
check("returns the real fill price", fill, 32.0)
check("and the short is flat", short[CE], 0)

# (b) first attempt will not fill -> cancel, re-price harder, fill on the next
c = ctrl(kite=StubKite(), mode="live")
c.marks[CE] = 30.0
c._CLOSE_POLL_S = 0.4
bufs_seen, cush_seen = [], []

def _price_spy(sym, side, buf=None, cushion_ticks=2):
    bufs_seen.append(buf)
    cush_seen.append(cushion_ticks)
    return 31.0 + (cushion_ticks or 0) * 0.05

c._limit_price = _price_spy
c._product_for = lambda sym, side: "NRML"
short = {CE: 65}
c.ledger.open_short_real = lambda sym: short.get(sym, 0)
c.ledger.record = lambda o: None
c.ledger.update_fill = lambda *a, **k: None
placed, cancels = [], []
kx.place_limit_verified = lambda *a, **k: (placed.append(1) or f"oid-{len(placed)}")
kx.cancel = lambda kite, oid: cancels.append(oid)
seq = iter(["OPEN", "COMPLETE"])                    # attempt1 won't fill, attempt2 does

def _status_seq(kite, oid):
    st = next(seq, "COMPLETE")
    if st == "COMPLETE":
        short[CE] = 0
        return {"status": "COMPLETE", "filled_qty": 65, "avg_price": 33.0, "fill_time": "11:00:06"}
    return {"status": st, "filled_qty": 0, "avg_price": None, "fill_time": None}

kx.order_status = _status_seq
fill = c._close_leg(CE, 1, "exit", 65)
check("re-placed after the miss", len(placed), 2)
check("the stuck order was cancelled", len(cancels), 1)
check("re-priced harder (cushion escalated)", cush_seen[1] > cush_seen[0], True)
check("closed on the retry", (fill, short[CE]), (33.0, 0))


print("\n  -- orphan guard: a short left on the book when we think we're flat --")
# Belt-and-suspenders for a close that still can't fill inside its in-call tries:
# if the strategy believes it is flat (no open cycle, trigger not in-position) yet a
# REAL short remains, every tick keeps covering it off fresh depth until it's gone.
c = ctrl(kite=StubKite(), mode="live")
c.marks[CE], c.marks[PE] = 30.0, 28.0
covered = []
c._close_leg = lambda sym, cycle, kind, qty: covered.append((sym, kind, qty))
c.ledger.open_shorts = lambda: {}                  # MTM/kill block is skipped
c.ledger.open_short_real = lambda sym: 65 if sym == CE else 0
c._open = None
c.trigger.in_pos = False
c.guard.killed = False
c.trigger.on_tick = lambda combined, hm: None
c._last_flatten_try = 0.0
c.on_tick(58.0, 30.0, 28.0, "11:00:00")
check("orphan naked short is covered off depth", covered, [(CE, "orphan_cover", 65)])
c.on_tick(58.0, 30.0, 28.0, "11:00:01")
check("orphan cover throttled ~2s", len(covered), 1)
c.ledger.open_short_real = lambda sym: 0
c._last_flatten_try = 0.0
c.on_tick(58.0, 30.0, 28.0, "11:00:05")
check("flat -> no orphan cover", len(covered), 1)

# a legitimately-HELD position (in a cycle / trigger in-position) is NEVER auto-covered
c3 = ctrl(kite=StubKite(), mode="live")
c3.marks[CE], c3.marks[PE] = 30.0, 28.0
cov3 = []
c3._close_leg = lambda sym, cycle, kind, qty: cov3.append(sym)
c3.ledger.open_shorts = lambda: {CE: 65, PE: 65}
c3.ledger.open_short_real = lambda sym: 65
c3.guard.check_mtm = lambda marks: (False, 0.0)
c3.guard.killed = False
c3.guard.must_square_off = lambda now: False
c3._open = {"cycle": 1}
c3.trigger.in_pos = True
c3.trigger.on_tick = lambda combined, hm: None
c3._last_flatten_try = 0.0
c3.on_tick(58.0, 30.0, 28.0, "11:00")          # HH:MM — reaches the time-square-off check
check("a legitimately-held position is NEVER auto-covered", cov3, [])


print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
