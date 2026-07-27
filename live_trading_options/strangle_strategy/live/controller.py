"""
live/controller.py — coordinates one index's live/paper-live strangle.
=====================================================================

Wires the pieces together for a day:  trigger_engine -> risk_guard -> (paper: record
simulated fills | live: kite_executor real orders) -> ledger -> live_state.json.

The Paper/Live toggle changes ONE thing: whether the trigger engine's decisions
become real Kite orders (mode='live') or simulated fills (mode='paper'). Everything
else — the existing V2 paper capture, charts, sheet — is untouched.

Fills are recorded PER LEG (CE + PE) in the own-book Ledger, using the current per-leg
LTPs at the fire moment (paper) or the real broker fills (live). Two simultaneous
MARKET orders per action; the combined premium is synthetic so it can't be one order.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live.ledger import Ledger, Order, SELL, BUY, COMPLETE
from live.risk_guard import RiskGuard
from live.trigger_engine import LiveTrigger

STATE_DIR = ROOT / "data" / "live_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def _t(s: str) -> dt.time:
    return dt.datetime.strptime(s, "%H:%M").time()


class LiveController:
    def __init__(self, index: str, date_str: str, ce_sym: str, pe_sym: str, dte,
                 *, lot_size: int, lots: int, max_cycles: int, mtm_stop: float,
                 entry_cutoff: str, square_off: str, mode: str = "paper", kite=None,
                 kite_syms: dict | None = None):
        self.index, self.date = index, date_str
        self.ce, self.pe, self.dte = ce_sym, pe_sym, dte
        self.lot_size, self.lots = lot_size, lots
        self.qty = lot_size * lots
        self.mode = mode                       # 'paper' | 'live'
        self.kite = kite
        self.kite_syms = kite_syms or {}       # {fyers_sym: {tradingsymbol, exchange}} for live
        self.ledger = Ledger()
        self.guard = RiskGuard(self.ledger, max_cycles=max_cycles, mtm_stop=mtm_stop,
                               entry_cutoff=entry_cutoff, square_off=square_off)
        self.trigger = LiveTrigger(self._enter, self._exit, max_entries=max_cycles,
                                   entry_cutoff=entry_cutoff, square_off=square_off)
        self.marks = {ce_sym: None, pe_sym: None}
        self.cycles: list[dict] = []           # per-cycle rows for the Live tab
        self._open = None                      # the currently-open cycle dict
        self._hm = "09:15"
        self._oid = 0
        self.events: list[dict] = []           # flat entry/exit log

    # ── inputs from the tick engine ───────────────────────────────────────
    def on_tick(self, combined: float, ce_ltp: float, pe_ltp: float, hm: str):
        self._hm = hm
        if ce_ltp is not None:
            self.marks[self.ce] = ce_ltp
        if pe_ltp is not None:
            self.marks[self.pe] = pe_ltp
        # MTM stop: continuously marked; on breach -> guard kills and we flatten
        if self.ledger.open_shorts():
            breached, _ = self.guard.check_mtm(self.marks)
            if breached:
                self._flatten("MTM stop")
        self.trigger.on_tick(combined, hm)

    def on_candle_close(self, candle: dict):
        self.trigger.on_candle_close(candle)
        self.persist()

    # ── trigger callbacks ────────────────────────────────────────────────
    def _now(self) -> dt.datetime:
        return dt.datetime.combine(dt.date.fromisoformat(self.date), _t(self._hm))

    def _enter(self, combined_trigger: float, cycle: int, reason: str):
        ok, why = self.guard.validate_entry(cycle, f"{self.date}-e{cycle}", self._now())
        if not ok:
            self.events.append({"t": self._hm, "type": "entry_blocked", "cycle": cycle, "why": why})
            return
        ce_fill = self._sell(self.ce, cycle)
        pe_fill = self._sell(self.pe, cycle)
        naked = self.guard.check_naked(self.ce, self.pe)   # a leg failed to fill?
        if naked:
            self._cover_naked(naked, cycle, "entry leg failed")
        self.guard.mark_fired(f"{self.date}-e{cycle}")
        combined = round((ce_fill or 0) + (pe_fill or 0), 2)
        self._open = {"cycle": cycle, "entry_time": self._hm, "entry_combined": combined,
                      "entry_ce": ce_fill, "entry_pe": pe_fill, "trigger": combined_trigger,
                      "exit_time": None, "exit_combined": None, "points": None, "pnl": None}
        self.cycles.append(self._open)
        self.events.append({"t": self._hm, "type": "entry", "cycle": cycle,
                            "combined": combined, "ce": ce_fill, "pe": pe_fill, "reason": reason})
        self.persist()

    def _exit(self, combined_price: float, cycle: int, reason: str):
        ce_out = self._buy(self.ce, cycle)
        pe_out = self._buy(self.pe, cycle)
        combined = round((ce_out or 0) + (pe_out or 0), 2)
        if self._open:
            pts = round((self._open["entry_combined"] or 0) - combined, 2)
            self._open.update(exit_time=self._hm, exit_combined=combined,
                              points=pts, pnl=round(pts * self.qty, 2))
            self._open = None
        self.events.append({"t": self._hm, "type": "exit", "cycle": cycle,
                            "combined": combined, "ce": ce_out, "pe": pe_out, "reason": reason})
        self.persist()

    # ── flatten (kill / square-off): buy back exactly the OWN open shorts ──
    def _flatten(self, reason: str):
        for sym in list(self.ledger.open_shorts()):
            self._buy(sym, self.guard.max_cycles, kind="square_off")
        if self._open:
            combined = round(sum(self.marks.get(s) or 0 for s in (self.ce, self.pe)), 2)
            pts = round((self._open["entry_combined"] or 0) - combined, 2)
            self._open.update(exit_time=self._hm, exit_combined=combined,
                              points=pts, pnl=round(pts * self.qty, 2))
            self._open = None
        self.events.append({"t": self._hm, "type": "flatten", "reason": reason})
        self.persist()

    def _cover_naked(self, naked, cycle, reason):
        sym, units = naked
        self.events.append({"t": self._hm, "type": "naked_cover", "symbol": sym,
                            "units": units, "reason": reason})
        self._buy(sym, cycle, kind="naked_cover", qty=units)

    # ── order placement: paper simulates; live goes through the executor ──
    def _sell(self, sym: str, cycle: int, qty: int = None) -> float:
        return self._place(sym, SELL, cycle, "entry", qty)

    def _buy(self, sym: str, cycle: int, kind: str = "exit", qty: int = None) -> float:
        return self._place(sym, BUY, cycle, kind, qty)

    def _place(self, sym: str, side: str, cycle: int, kind: str, qty: int = None) -> float:
        qty = qty or self.qty
        if self.mode == "live":
            return self._place_live(sym, side, cycle, kind, qty)
        # paper: simulated fill at the current LTP of this leg
        self._oid += 1
        oid = f"paper-{kind}-{self._oid}"
        fill = self.marks.get(sym)
        o = Order(oid, sym, side, qty, cycle, kind)
        self.ledger.record(o)
        self.ledger.update_fill(oid, COMPLETE, filled_qty=qty, avg_price=fill)
        return fill

    def _place_live(self, sym: str, side: str, cycle: int, kind: str, qty: int) -> float:
        """LIVE: fire a real MARKET order via the executor, poll the fill, book it.
        Kite symbol comes from the pre-resolved kite_syms map (never hand-built)."""
        from live import kite_executor as kx
        import time
        ks = self.kite_syms[sym]
        oid = kx.place_market(self.kite, ks["tradingsymbol"], ks["exchange"], side, qty)
        o = Order(oid, sym, side, qty, cycle, kind)
        self.ledger.record(o)
        fill = None
        for _ in range(10):                    # poll up to ~5s for the fill
            st = kx.order_status(self.kite, oid)
            self.ledger.update_fill(oid, st["status"], st.get("filled_qty"), st.get("avg_price"))
            if st["status"] in ("COMPLETE", "REJECTED", "CANCELLED"):
                fill = st.get("avg_price")
                break
            time.sleep(0.5)
        return fill

    # ── state for the Live tab ────────────────────────────────────────────
    def snapshot(self) -> dict:
        realized = sum(c["pnl"] for c in self.cycles if c["pnl"] is not None)
        mtm = self.ledger.mtm({k: v for k, v in self.marks.items() if v is not None})
        return {"index": self.index, "date": self.date, "mode": self.mode, "dte": self.dte,
                "ce_symbol": self.ce, "pe_symbol": self.pe, "qty": self.qty,
                "killed": self.guard.killed, "kill_reason": self.guard.kill_reason,
                "open": self._open, "cycles": self.cycles, "events": self.events,
                "marks": self.marks, "realized_pnl": round(realized, 2), "mtm_pnl": mtm,
                "reconcile": self.guard.check_reconcile(),
                "updated": dt.datetime.now().strftime("%H:%M:%S")}

    def persist(self):
        (STATE_DIR / f"{self.date}_{self.index}_LIVE.json").write_text(
            json.dumps(self.snapshot(), indent=2))
