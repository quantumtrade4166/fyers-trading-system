"""core/test_chain_reload.py — engine.chain_for() re-reads the product master.

2026-09-19: the 19-Sep chain was built from the product master at 17:35 the day
before. Delta listed higher strikes as BTC rose, but the engine only refreshed
the master once per IST date, so a cached chain never learned of them. Now the
master refreshes every 10 minutes (and right before selection) and any cached
chain older than the master is re-loaded — keeping its marks and positions.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import datetime as dt
import engine
from core.selector import CE, PE

PASS = FAIL = 0
def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")

EXP = "190926"
def rows(top):
    return [{"symbol": f"{c}-BTC-{k}-{EXP}", "id": k * 2 + (c == "P"), "strike_price": str(k),
             "contract_value": "0.001", "tick_size": "0.1"}
            for k in range(76000, top + 1, 200) for c in ("C", "P")]

master = {"rows": rows(80000), "calls": 0}
def fake_btc_options():
    master["calls"] += 1
    return master["rows"]
engine.btc_options = fake_btc_options
engine.log = lambda msg: None
engine._chains.clear(); engine._chain_loaded.clear()
engine._products.update(rows=[], at=0.0, gen=0)

ch = engine.chain_for(EXP)
check("built from the first master", ch.strikes[-1], 80000.0)
ch.mark[(80000.0, CE)] = 123.0          # a mark the chain already holds

# Delta lists higher strikes; within 10 minutes the cached chain is unchanged
master["rows"] = rows(82000)
engine.products()
check("no refetch inside the interval", master["calls"], 1)
check("cached chain unchanged inside the interval", engine.chain_for(EXP).strikes[-1], 80000.0)

# the interval elapses -> master refetched -> cached chain re-loaded in place
engine._products["at"] -= engine.PRODUCTS_EVERY + 1
engine.products()
ch2 = engine.chain_for(EXP)
check("same chain object (positions keep their chain)", ch2 is ch, True)
check("new strikes picked up", ch2.strikes[-1], 82000.0)
check("existing marks kept", ch2.mark.get((80000.0, CE)), 123.0)
check("new contract tradeable", ch2.symbol_for(81600, CE), f"C-BTC-81600-{EXP}")

# a forced refresh (pre-selection) is picked up immediately
master["rows"] = rows(83000)
engine.products(force=True)
check("forced refresh reaches the chain", engine.chain_for(EXP).strikes[-1], 83000.0)

# a failed routine refresh keeps the old rows instead of failing the poll
def boom():
    raise RuntimeError("network down")
engine.btc_options = boom
engine._products["at"] -= engine.PRODUCTS_EVERY + 1
check("routine failure keeps rows", len(engine.products()), len(rows(83000)))
try:
    engine.products(force=True); check("forced failure raises", False, True)
except RuntimeError:
    check("forced failure raises", True, True)

# selection_due: true at entry and at an un-run window, false otherwise
class FakeCtrl:
    pass
from core.sessions import load_profiles
from live.controller import BTCController
prof = load_profiles(engine.PARAMS)
name = next(iter(prof))
c = BTCController(prof[name], engine.PARAMS, out_dir=Path(__import__("tempfile").mkdtemp()))
p = prof[name]
# find this profile's entry moment from a noon/evening probe
probe = dt.datetime(2026, 9, 21, 12, 0)
b = p.cycle_bounds(probe) or p.cycle_bounds(dt.datetime(2026, 9, 21, 20, 0))
entry = b[0]
check("due at cycle entry", c.selection_due(entry + dt.timedelta(seconds=5)), True)
c.cycle = p.cycle_key(entry); c.entered = True
check("not due once entered", c.selection_due(entry + dt.timedelta(seconds=30)), False)
w = p.window_starts(entry)[0]
check("due at an un-run window", c.selection_due(w + dt.timedelta(seconds=2)), True)
c.done_windows.add(f"{w:%Y-%m-%dT%H:%M}")
check("not due once the window ran", c.selection_due(w + dt.timedelta(seconds=2)), False)
check("not due after square-off", c.selection_due(b[1] + dt.timedelta(minutes=1)), False)

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
