"""test_kotak_exit_safety.py — Kotak mirror: never-give-up, depth-driven exit.

OFFLINE. Builds a KotakController with a stub client; places no real orders. The Kotak
twin of test_exit_safety.py. Covers the fix for the naked-leg-on-exit bug: a close must
keep re-pricing off Kotak's LIVE depth and lifting deeper until the real short is flat,
and a short left on the book when the mirror thinks it is flat is swept every tick.

    .venv\\Scripts\\python.exe live_trading_options/strangle_strategy/live/test_kotak_exit_safety.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))               # strangle_strategy/ (so `live.` resolves)

from live.kotak_controller import KotakController
from live import kotak_executor as ke

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
SYMS = {CE: {"trading_symbol": "NIFTY26AUG24500CE", "exchange_segment": "nse_fo",
             "lot_size": 65, "token": "111"},
        PE: {"trading_symbol": "NIFTY26AUG24200PE", "exchange_segment": "nse_fo",
             "lot_size": 65, "token": "222"}}


class StubKotak:
    """Places nothing, serves nothing — the ke.* functions are monkeypatched per test."""


def ctrl(mode="live"):
    c = KotakController("NIFTY", "2026-08-27", CE, PE, 0,
                        lot_size=65, lots=1, max_cycles=4, mtm_stop=16000,
                        entry_cutoff="14:30", square_off="15:14",
                        kotak=StubKotak(), kotak_syms=SYMS)
    c._check_control = lambda: None
    c._write_tick = lambda combined: None
    c.persist = lambda: None
    c.mode = mode
    return c


# save the real ke functions so each test restores a clean slate
_REAL = {k: getattr(ke, k) for k in ("place_limit", "order_status", "cancel")}


def _restore():
    for k, v in _REAL.items():
        setattr(ke, k, v)


print("\n  -- a close NEVER gives up: re-prices off depth until the short is flat --")
# The 2026-09-07/09-30 pattern on the Kotak mirror: an exit's buy-back misses the fill
# and the short leg is left naked. A close must keep lifting deeper into the book.

# (a) fills on the first attempt -> one placement, no cancel
c = ctrl()
c._CLOSE_POLL_S = 0.4
c._leg_price = lambda sym, side, qty, buf, cushion_ticks=2: 31.0 + cushion_ticks * 0.05
short = {CE: 65}
c.ledger.open_short_real = lambda s: short.get(s, 0)
c.ledger.record = lambda o: None
c.ledger.update_fill = lambda *a, **k: None
placed, cancels = [], []
ke.place_limit = lambda *a, **k: (placed.append(1) or f"koid-{len(placed)}")
ke.cancel = lambda client, oid: cancels.append(oid)


def _status_fill_now(client, oid):
    short[CE] = 0
    return {"status": "complete", "filled_qty": 65, "avg_price": 32.0, "fill_time": "11:00:05"}


ke.order_status = _status_fill_now
fill = c._close_leg(CE, 1, "exit", 65)
check("filled on attempt 1", (len(placed), len(cancels)), (1, 0))
check("returns the real fill price", fill, 32.0)
check("and the short is flat", short[CE], 0)

# (b) first attempt will not fill -> cancel, re-price harder, fill on the next
c = ctrl()
c._CLOSE_POLL_S = 0.4
cush_seen = []


def _price_spy(sym, side, qty, buf, cushion_ticks=2):
    cush_seen.append(cushion_ticks)
    return 31.0 + cushion_ticks * 0.05


c._leg_price = _price_spy
short = {CE: 65}
c.ledger.open_short_real = lambda s: short.get(s, 0)
c.ledger.record = lambda o: None
c.ledger.update_fill = lambda *a, **k: None
placed, cancels = [], []
ke.place_limit = lambda *a, **k: (placed.append(1) or f"koid-{len(placed)}")
ke.cancel = lambda client, oid: cancels.append(oid)
seq = iter([("open", 0, None),        # attempt 1 poll: not filled
            ("open", 0, None),        # attempt 1 settle_unfilled check: still not filled
            ("complete", 65, 33.0)])  # attempt 2 poll: filled


def _status_seq(client, oid):
    s, fq, ap = next(seq, ("complete", 65, 33.0))
    if s == "complete":
        short[CE] = 0
    return {"status": s, "filled_qty": fq, "avg_price": ap, "fill_time": "11:00:06"}


ke.order_status = _status_seq
fill = c._close_leg(CE, 1, "exit", 65)
check("re-placed after the miss", len(placed), 2)
check("the stuck order was cancelled", len(cancels), 1)
check("re-priced harder (cushion escalated)", cush_seen[1] > cush_seen[0], True)
check("closed on the retry", (fill, short[CE]), (33.0, 0))
_restore()


print("\n  -- orphan guard: a short left on the book when we think we're flat --")
c = ctrl()
c.marks[CE], c.marks[PE] = 30.0, 28.0
covered = []
c._close_leg = lambda sym, cycle, kind, qty: covered.append((sym, kind, qty))
c.ledger.open_shorts = lambda: {}                  # MTM/kill block is skipped
c.ledger.open_short_real = lambda s: 65 if s == CE else 0
c._open = None
c.trigger.in_pos = False
c.guard.killed = False
c.trigger.on_tick = lambda combined, hm: None
c._last_flatten_try = 0.0
c.on_tick(58.0, 30.0, 28.0, "11:00")
check("orphan naked short is covered off depth", covered, [(CE, "orphan_cover", 65)])
c.on_tick(58.0, 30.0, 28.0, "11:00")
check("orphan cover throttled ~2s", len(covered), 1)
c.ledger.open_short_real = lambda s: 0
c._last_flatten_try = 0.0
c.on_tick(58.0, 30.0, 28.0, "11:00")
check("flat -> no orphan cover", len(covered), 1)

# a legitimately-HELD position is NEVER auto-covered
c3 = ctrl()
c3.marks[CE], c3.marks[PE] = 30.0, 28.0
cov3 = []
c3._close_leg = lambda sym, cycle, kind, qty: cov3.append(sym)
c3.ledger.open_shorts = lambda: {CE: 65, PE: 65}
c3.ledger.open_short_real = lambda s: 65
c3.guard.check_mtm = lambda marks: (False, 0.0)
c3.guard.killed = False
c3.guard.must_square_off = lambda now: False
c3._open = {"cycle": 1}
c3.trigger.in_pos = True
c3.trigger.on_tick = lambda combined, hm: None
c3._last_flatten_try = 0.0
c3.on_tick(58.0, 30.0, 28.0, "11:00")
check("a legitimately-held position is NEVER auto-covered", cov3, [])


print("\n  -- Kotak sends prices as STRINGS: they must be coerced before any math --")
# The 2026-10-01 SENSEX incident: Bhaiya placed both legs, they filled, but Kotak
# returned avg_price as "32.0" (a string). The ledger's `avg_price * qty` then did
# str*int and `sum()` did 0 + str -> "unsupported operand type(s) for +: 'int' and
# 'str'" on EVERY tick after the fill, crashing the controller so it could neither
# confirm nor manage the position. order_status/strategy_fills must return floats.
from live.ledger import Ledger, Order, SELL, COMPLETE


class StubReport:
    def __init__(self, rows):
        self._rows = rows

    def order_report(self):
        return {"data": self._rows}


row = {"nOrdNo": "OID1", "ordSt": "complete", "fldQty": "20", "avgPrc": "32.0",
       "trdSym": "SENSEX26O0173000CE", "trnsTp": "S", "flDtTm": "09:37:14"}
st = ke.order_status(StubReport([row]), "OID1")
check("order_status coerces avg_price to float", isinstance(st["avg_price"], float), True)
check("avg_price value preserved", st["avg_price"], 32.0)
sf = ke.strategy_fills(StubReport([{**row, "tag": "vwsk093700001"}]))
check("strategy_fills coerces avg_price to float", isinstance(sf[0]["avg_price"], float), True)

# the exact crash site: book that fill and run the ledger math that ran every tick
L = Ledger()
L.record(Order("OID1", "CE", SELL, 20, 1, "entry"))
L.update_fill("OID1", COMPLETE, filled_qty=20, avg_price=st["avg_price"], fill_time="09:37")
try:
    cash = L.cash()
    mtm = L.mtm({"CE": 30.0})
    ok = isinstance(cash, float) and isinstance(mtm, float)
except TypeError:
    ok = False
check("ledger.cash()/mtm() no longer crash on a Kotak fill", ok, True)


print("\n  -- Rohit subclass inherits the same never-naked close --")
from live.kotak_rohit_controller import KotakRohitController
check("Rohit does not override _close_leg", KotakRohitController._close_leg is KotakController._close_leg, True)
check("Rohit does not override _place", KotakRohitController._place is KotakController._place, True)
check("Rohit inherits the orphan-guard on_tick", KotakRohitController.on_tick is KotakController.on_tick, True)


print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
