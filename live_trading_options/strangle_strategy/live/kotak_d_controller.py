"""
live/kotak_d_controller.py — INDEPENDENT Kotak Neo mirror for the "Kotak D" account.
====================================================================================

A FOURTH, fully independent trading engine (alongside Zerodha, Kotak Bhaiya, Kotak Rohit)
for the VWAP strangle. Reuses the SAME broker-agnostic modules (Ledger, RiskGuard,
LiveTrigger) so its signal is identical to the others, but it places its OWN NRML orders
on a SEPARATE Kotak session (its own mobile, UCC, TOTP) and keeps its OWN guard / kill /
ledger / snapshot / control flags.

INDEPENDENCE:
  - Own per-broker arm switch: KOTAK_D_{INDEX} (separate from {INDEX}, KOTAK_{INDEX},
    KOTAK_ROHIT_{INDEX}).
  - Own guard.killed / trigger.done: a Kotak D order failure kills ONLY Kotak D.
  - Own snapshot file: {date}_{index}_KOTAK_D.json
  - Own tick file: {date}_{index}_KOTAK_D_TICK.json
  - Own order tag prefix: vwskd (distinct from vwsk / vwsk2, so accounts never collide).

Minimal subclass of KotakController (like the Rohit mirror): swaps only the flag prefix,
tag prefix, file infix, and account label. ALL trading logic — two-leg fire, never-naked
_close_leg, orphan guard, the broker-verify fail-safe, retry, reconcile, depth pricing,
Kotak string-price coercion — is inherited 1:1 from the working KotakController.
"""

import sys
import json
import time as _time
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live.ledger import Ledger, Order, SELL, BUY, COMPLETE, CANCELLED
from live.risk_guard import RiskGuard
from live.trigger_engine import LiveTrigger
from live import audit
from live.kotak_controller import KotakController
from live import kotak_executor as ke

STATE_DIR = ROOT / "data" / "live_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
TAG_PREFIX = "vwskd"                                   # unique to Kotak D (Bhaiya=vwsk, Rohit=vwsk2)
_ctrl_prefix = "KOTAK_D_"                              # control flag prefix
_snap_infix = "_KOTAK_D"                              # file name infix


class KotakDController(KotakController):
    """Drop-in replacement for KotakController targeting the Kotak D account.

    Inherits ALL trading logic (entry, exit, two-leg fire, never-naked close, broker-verify
    fail-safe, retry, reconcile, pricing). Overrides only: control-flag prefix, snapshot/tick
    file naming, order tag prefix, and the audit label.
    """

    tag_prefix = TAG_PREFIX          # "vwskd" — every Kotak D order + its reconcile lookup

    def _check_control(self):
        now = _time.monotonic()
        if now - self._last_ctrl < 1.0:
            return
        self._last_ctrl = now
        try:
            from live.control_flags import read_control
            c = read_control(f"{_ctrl_prefix}{self.index}")
        except Exception:
            return
        if not self.ledger.open_shorts():
            q = c.get("qty")
            if isinstance(q, (int, float)) and q > 0 and q % self.lot_size == 0 \
                    and (q // self.lot_size) <= 15 and int(q) != self.qty:
                self.qty = int(q); self.lots = int(q) // self.lot_size
            m = c.get("mtm_stop")
            if isinstance(m, (int, float)) and m > 0:
                self.guard.mtm_stop = abs(float(m))
        nm = c.get("mode")
        if nm in ("paper", "live"):
            if nm != self.mode:
                audit.log(self.index, "KOTAK_D_ARM" if nm == "live" else "KOTAK_D_DISARM",
                          mode=nm, dte=self.dte, qty=self.qty, mtm_stop=self.guard.mtm_stop)
            self.mode = nm
        if c.get("kill") and not self.guard.killed:
            audit.log(self.index, "KOTAK_D_KILL", open_shorts=len(self.ledger.open_shorts()))
            self.guard.kill("kill switch")
            if self.ledger.open_shorts():
                self._flatten("kill switch")
            else:
                self.trigger.done = True

    def _fire_leg(self, sym, side, cycle, kind, buf):
        """Place ONE marketable leg under Kotak D's unique tag prefix (vwskd) so its orders
        on the Kotak D account are always distinguishable from Bhaiya's and Rohit's."""
        ks = self.kotak_syms[sym]
        price = self._leg_price(sym, side, self.qty, buf)
        oid = ke.place_limit(self.kotak, ks["trading_symbol"], ks["exchange_segment"],
                             side, self.qty, price, tag=self.tag_prefix)
        self.ledger.record(Order(oid, sym, side, self.qty, cycle, kind))
        audit.log(self.index, "KOTAK_D_ORDER_PLACED", cyc=cycle, side=side,
                  sym=ks["trading_symbol"], qty=self.qty, oid=oid, limit=price)
        return {"oid": oid, "fill": None}

    def _write_tick(self, combined):
        now = _time.monotonic()
        if now - self._last_tick_write < 0.4:
            return
        self._last_tick_write = now
        try:
            mtm = self.ledger.mtm({k: v for k, v in self.marks.items() if v is not None})
            (STATE_DIR / f"{self.date}_{self.index}{_snap_infix}_TICK.json").write_text(
                json.dumps({
                    "t": self._hm, "combined": round(combined, 2), "mtm": mtm,
                    "ce": self.marks.get(self.ce), "pe": self.marks.get(self.pe),
                    "armed": self.is_live_armed(),
                    "updated": dt.datetime.now().strftime("%H:%M:%S")}))
        except Exception:
            pass

    def snapshot(self):
        realized = sum(c["pnl"] for c in self.cycles if c["pnl"] is not None)
        mtm = self.ledger.mtm({k: v for k, v in self.marks.items() if v is not None})
        return {"margin_halt": self.margin_halt,
                "index": self.index, "date": self.date, "broker": "KOTAK_D",
                "mode": self.mode, "dte": self.dte, "armed": self.is_live_armed(),
                "trades_allowed": self._trades_allowed,
                "broker_ready": bool(self.kotak and self.kotak_syms),
                "ce_symbol": self.ce, "pe_symbol": self.pe, "qty": self.qty,
                "killed": self.guard.killed, "kill_reason": self.guard.kill_reason,
                "open": self._open, "cycles": self.cycles, "events": self.events,
                "orders": [o.to_dict() for o in self.ledger.orders.values()],
                "marks": self.marks, "realized_pnl": round(realized, 2), "mtm_pnl": mtm,
                "mtm_series": self._mtm_series, "reconcile": self.guard.check_reconcile(),
                "updated": dt.datetime.now().strftime("%H:%M:%S")}

    def persist(self):
        try:
            (STATE_DIR / f"{self.date}_{self.index}{_snap_infix}.json").write_text(
                json.dumps(self.snapshot(), indent=2))
        except Exception:
            pass

    def reconcile_kotak(self):
        """Restart-safe recovery from REAL Kotak D fills. TAG-SCOPED to `vwskd*`."""
        if not (self.kotak and self.kotak_syms):
            return
        try:
            from live.control_flags import read_control
            m = read_control(f"{_ctrl_prefix}{self.index}").get("mode")
            if m in ("paper", "live"):
                self.mode = m
        except Exception:
            pass
        ts_to_fy = {v["trading_symbol"]: fy for fy, v in self.kotak_syms.items()}
        try:
            fills = ke.strategy_fills(self.kotak, tag_prefix=self.tag_prefix)
        except Exception as e:
            audit.log(self.index, "KOTAK_D_RECONCILE_FAIL", error=str(e))
            return

        def _f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        mine = []
        for f in fills:
            fy = ts_to_fy.get(f.get("trading_symbol"))
            if fy not in (self.ce, self.pe):
                continue
            mine.append({"order_id": str(f["order_id"]), "fy": fy,
                         "side": SELL if str(f["side"]).upper().startswith("S") else BUY,
                         "qty": int(f["qty"] or 0), "avg_price": _f(f["avg_price"]),
                         "fill_time": f.get("fill_time")})
        mine.sort(key=lambda f: (f.get("fill_time") or ""))

        if self.is_live_armed():
            self.ledger = Ledger()
            self.guard.L = self.ledger
            for f in mine:
                self.ledger.record(Order(f["order_id"], f["fy"], f["side"], f["qty"], 0, "reconciled"))
                self.ledger.update_fill(f["order_id"], COMPLETE, f["qty"], f["avg_price"], f["fill_time"])
            self._rebuild_cycles_from_fills(mine)
        else:
            for f in mine:
                if f["order_id"] in self.ledger.orders:
                    continue
                self.ledger.record(Order(f["order_id"], f["fy"], f["side"], f["qty"], 0, "reconciled"))
                self.ledger.update_fill(f["order_id"], COMPLETE, f["qty"], f["avg_price"], f["fill_time"])

        ce_s, pe_s = self.ledger.open_short_real(self.ce), self.ledger.open_short_real(self.pe)
        audit.log(self.index, "KOTAK_D_RECONCILE", added=len(mine), ce_short=ce_s, pe_short=pe_s,
                  in_pos=self.trigger.in_pos, mode=self.mode)
        self.persist()
