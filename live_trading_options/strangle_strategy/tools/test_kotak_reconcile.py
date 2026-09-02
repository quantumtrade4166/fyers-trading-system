"""tools/test_kotak_reconcile.py — offline test of KotakController.reconcile_kotak.

Stubs the Kotak client + fills (NO login, NO orders). Proves that after a restart the mirror
recovers its REAL position from tagged fills: open short recovered, cycles/equity rebuilt,
guard re-pointed at the live book, and a closed round-trip reconstructs P&L. Also proves the
paper seed-replay short is DROPPED in live mode (no double-count)."""
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live import kotak_controller as kc
from live import kotak_executor as ke
from live import control_flags as cf
from live.ledger import Order, SELL, COMPLETE

CE, PE = "NSE:NIFTY-CE", "NSE:NIFTY-PE"
SYMS = {CE: {"trading_symbol": "NIFTYCE", "exchange_segment": "nse_fo", "lot_size": 65},
        PE: {"trading_symbol": "NIFTYPE", "exchange_segment": "nse_fo", "lot_size": 65}}
_pass = _fail = 0


def chk(name, cond):
    global _pass, _fail
    print(("OK   " if cond else "FAIL ") + name)
    _pass += cond
    _fail += (not cond)


def ctrl():
    return kc.KotakController("NIFTY", "2026-09-08", CE, PE, 0, lot_size=65, lots=1,
                             max_cycles=4, mtm_stop=1000, entry_cutoff="14:30",
                             square_off="15:14", kotak=object(), kotak_syms=SYMS)


def stub(fills, mode="live"):
    ke.strategy_fills = lambda client, tag="vwstk_kotak": fills
    cf.read_control = lambda name: {"mode": mode}


SELL_PAIR = [
    {"trading_symbol": "NIFTYCE", "side": "S", "qty": 65, "avg_price": "100.0", "order_id": "o1", "fill_time": "09:25:00"},
    {"trading_symbol": "NIFTYPE", "side": "S", "qty": 65, "avg_price": "120.0", "order_id": "o2", "fill_time": "09:25:01"},
]
BUY_PAIR = [
    {"trading_symbol": "NIFTYCE", "side": "B", "qty": 65, "avg_price": "80.0", "order_id": "o3", "fill_time": "10:00:00"},
    {"trading_symbol": "NIFTYPE", "side": "B", "qty": 65, "avg_price": "90.0", "order_id": "o4", "fill_time": "10:00:01"},
]

# ── A. restart while LIVE with an open short — recover it ──────────────────────
print("\n[A] live restart, open short:")
c = ctrl()
c.ledger.record(Order("paper-k-entry-1", CE, SELL, 65, 1, "entry"))     # a stale paper seed short
c.ledger.update_fill("paper-k-entry-1", COMPLETE, 65, 99.0, "09:20:00")
stub(SELL_PAIR, "live")
c.reconcile_kotak()
chk("mode primed to live", c.mode == "live")
chk("CE real short = 65", c.ledger.open_short_real(CE) == 65)
chk("PE real short = 65", c.ledger.open_short_real(PE) == 65)
chk("paper seed short dropped (no double)", c.ledger.open_short(CE) == 65)
chk("guard.L re-pointed to live ledger", c.guard.L is c.ledger)
chk("trigger.in_pos = True", c.trigger.in_pos is True)
chk("one open cycle", len(c.cycles) == 1 and c.cycles[0]["exit_combined"] is None)
chk("entry_combined = 220.0", c.cycles[0]["entry_combined"] == 220.0)
chk("guard MTM uses live book", c.guard.check_mtm({CE: 100.0, PE: 120.0})[1] == 0.0)

# ── B. closed round-trip — reconstruct P&L ────────────────────────────────────
print("\n[B] live restart, closed round-trip:")
c = ctrl()
stub(SELL_PAIR + BUY_PAIR, "live")
c.reconcile_kotak()
chk("flat: CE short = 0", c.ledger.open_short_real(CE) == 0)
chk("flat: PE short = 0", c.ledger.open_short_real(PE) == 0)
chk("trigger.in_pos = False", c.trigger.in_pos is False)
chk("cycle closed, points = 50", c.cycles[0]["points"] == 50.0)
chk("cycle pnl = 50*65 = 3250", c.cycles[0]["pnl"] == 3250.0)
chk("equity curve has 2 points", len(c._mtm_series) == 2)

# ── C. paper mode — no real orders, reconcile is a no-op ──────────────────────
print("\n[C] paper mode, no real fills:")
c = ctrl()
stub([], "paper")
c.reconcile_kotak()
chk("mode stays paper", c.mode == "paper")
chk("no position", not c.ledger.open_shorts())

print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
