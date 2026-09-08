"""tools/test_kotak_naked.py — offline test of KotakController's never-leave-a-naked-leg entry.

Stubs the Kotak executor (NO login, NO real orders). Proves the mirror behaves like Zerodha:
  A) both legs fill  -> normal live entry, not killed;
  B) one leg never fills (the 2026-09-07 case, when the PE was rejected as a duplicate tag) ->
     the laggard is retried then CANCELLED and the FILLED leg is auto-covered, so we end FLAT
     with the guard killed — never a resting order or a naked short.
Run: .venv\\Scripts\\python.exe live_trading_options/strangle_strategy/tools/test_kotak_naked.py
"""
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live import kotak_controller as kc
from live import kotak_executor as ke
from live import control_flags as cf

CE, PE = "NSE:NIFTY-CE", "NSE:NIFTY-PE"
SYMS = {CE: {"trading_symbol": "NIFTYCE", "exchange_segment": "nse_fo", "lot_size": 65},
        PE: {"trading_symbol": "NIFTYPE", "exchange_segment": "nse_fo", "lot_size": 65}}
cf.read_control = lambda name: {"mode": "live"}

_n = [0]
_orders = {}


def _place(client, ts, seg, side, qty, price, product="NRML", tag="vwsk"):
    _n[0] += 1
    oid = f"K{_n[0]}"
    _orders[oid] = (ts, side)
    return oid


def _mk_status(fill_pe):
    def order_status(client, oid):
        ts, side = _orders[oid]
        is_sell = str(side).upper().startswith("S")
        filled = not (ts == "NIFTYPE" and is_sell and not fill_pe)      # PE SELL is the laggard
        if filled:
            return {"status": "complete", "filled_qty": 65,
                    "avg_price": 100.0 if ts == "NIFTYCE" else 120.0, "fill_time": "09:25:01"}
        return {"status": "open", "filled_qty": 0, "avg_price": None, "fill_time": None}
    return order_status


def run(fill_pe):
    _n[0] = 0
    _orders.clear()
    ke.place_limit = _place
    ke.order_status = _mk_status(fill_pe)
    ke.cancel = lambda client, oid: {"stat": "Ok"}
    c = kc.KotakController("NIFTY", "2026-09-08", CE, PE, 0, lot_size=65, lots=1, max_cycles=4,
                           mtm_stop=1000, entry_cutoff="14:30", square_off="15:14",
                           kotak=object(), kotak_syms=SYMS)
    c.mode = "live"
    c._hm = "09:25"
    c.marks = {CE: 100.0, PE: 120.0}
    c._enter(220.0, 1, "test")
    return c


_pass = _fail = 0


def chk(name, cond):
    global _pass, _fail
    print(("OK   " if cond else "FAIL ") + name)
    _pass += bool(cond)
    _fail += (not cond)


print("[A] both legs fill -> normal entry:")
a = run(fill_pe=True)
chk("CE short = 65", a.ledger.open_short_real(CE) == 65)
chk("PE short = 65", a.ledger.open_short_real(PE) == 65)
chk("not killed", not a.guard.killed)
chk("open cycle", a._open is not None)

print("\n[B] PE never fills -> auto-cover, no naked (the 2026-09-07 case):")
b = run(fill_pe=False)
chk("CE leg auto-covered (short 0)", b.ledger.open_short_real(CE) == 0)
chk("PE short 0", b.ledger.open_short_real(PE) == 0)
chk("guard killed", b.guard.killed)
chk("emitted naked_cover", any(e["type"] == "naked_cover" for e in b.events))
chk("emitted entry_incomplete", any(e["type"] == "entry_incomplete" for e in b.events))

print("\n[C] marketable_limit direction (the 2026-09-08 unfilled-sell fix):")
s = [ke.marketable_limit(10.45, "SELL", b) for b in (0.30, 0.60, 0.90)]
b = [ke.marketable_limit(10.45, "BUY", b) for b in (0.30, 0.60, 0.90)]
chk("SELL priced BELOW mark, more aggressive = lower", all(p < 10.45 for p in s) and s[0] > s[-1])
chk("BUY priced ABOVE mark, more aggressive = higher", all(p > 10.45 for p in b) and b[0] < b[-1])
chk("ledger word 'SELL' == Kotak code 'S'",
    ke.marketable_limit(10.45, "SELL", 0.3) == ke.marketable_limit(10.45, "S", 0.3))

print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
