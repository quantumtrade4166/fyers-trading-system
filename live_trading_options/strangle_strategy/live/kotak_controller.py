"""
live/kotak_controller.py — INDEPENDENT Kotak Neo mirror engine for the strangle.
===============================================================================

A SECOND, fully independent trading engine that runs ALONGSIDE the Zerodha controller.
It reuses the broker-agnostic strategy modules (LiveTrigger, RiskGuard, Ledger) so its
SIGNAL is identical to Zerodha's — fed the same ticks/candles from the shared Fyers feed —
but it places its OWN 1-lot NRML orders on Kotak (via kotak_executor) and keeps its OWN
guard / kill / ledger / snapshot.

INDEPENDENCE (the whole point):
  - Own per-broker arm switch: control flag KOTAK_{INDEX} (separate from Zerodha's {INDEX}).
  - Own guard.killed / trigger.done: a Kotak order failure kills ONLY Kotak; Zerodha runs on,
    and vice-versa. The tick-engine tap wraps EACH controller in its own try/except, so a
    Kotak exception can never reach the Zerodha controller or the capture loop.
  - Own snapshot file: {date}_{index}_KOTAK.json (never overwrites the Zerodha LIVE.json).

This module does NOT import or touch live.controller — the battle-tested Zerodha path is
left exactly as-is.
"""

import sys
import json
import time as _time
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live.ledger import Ledger, Order, SELL, BUY, COMPLETE
from live.risk_guard import RiskGuard
from live.trigger_engine import LiveTrigger
from live import audit
from live import kotak_executor as ke

STATE_DIR = ROOT / "data" / "live_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
TAG = "vwstk_kotak"


class KotakController:
    def __init__(self, index, date_str, ce_sym, pe_sym, dte, *, lot_size, lots,
                 max_cycles, mtm_stop, entry_cutoff, square_off,
                 kotak=None, kotak_syms=None):
        self.index, self.date = index, date_str
        self.ce, self.pe, self.dte = ce_sym, pe_sym, dte
        self.lot_size, self.lots = lot_size, lots
        self.qty = lot_size * lots
        self.mode = "paper"                     # 'paper' | 'live' from the KOTAK_{index} flag
        self.kotak = kotak                      # the NeoAPI client (None -> paper only)
        self.kotak_syms = kotak_syms or {}      # {fy_sym: {trading_symbol, exchange_segment, lot_size}}
        self._gcfg = dict(max_cycles=max_cycles, mtm_stop=mtm_stop,
                          entry_cutoff=entry_cutoff, square_off=square_off)
        self._seeding = False
        self.ledger = Ledger()
        self.guard = RiskGuard(self.ledger, **self._gcfg)
        self.trigger = LiveTrigger(self._enter, self._exit, max_entries=max_cycles,
                                   entry_cutoff=entry_cutoff, square_off=square_off)
        self.marks = {ce_sym: None, pe_sym: None}
        self.cycles, self.events = [], []
        self._open = None
        self._hm = "09:15"
        self._oid = 0
        self._cum_pv = self._cum_vol = 0.0
        self._mtm_series = []
        self._last_fill_time = {}
        self._last_ctrl = 0.0
        self._last_tick_write = 0.0
        self._trades_allowed = dte in (0, 1)

    # ── arm switch: KOTAK-prefixed control flag (independent of Zerodha) ──────
    def is_live_armed(self) -> bool:
        return self.mode == "live"

    def _now(self) -> dt.datetime:
        return dt.datetime.combine(dt.date.fromisoformat(self.date),
                                   dt.datetime.strptime(self._hm, "%H:%M").time())

    def _check_control(self):
        now = _time.monotonic()
        if now - self._last_ctrl < 1.0:
            return
        self._last_ctrl = now
        try:
            from live.control_flags import read_control
            c = read_control(f"KOTAK_{self.index}")
        except Exception:
            return
        if not self.ledger.open_shorts():                 # size/stop only while flat
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
                audit.log(self.index, "KOTAK_ARM" if nm == "live" else "KOTAK_DISARM",
                          mode=nm, dte=self.dte, qty=self.qty, mtm_stop=self.guard.mtm_stop)
            self.mode = nm
        if c.get("kill") and not self.guard.killed:
            audit.log(self.index, "KOTAK_KILL", open_shorts=len(self.ledger.open_shorts()))
            self.guard.kill("kill switch")
            if self.ledger.open_shorts():
                self._flatten("kill switch")
            else:
                self.trigger.done = True

    # ── inputs from the shared tick engine (same feed as Zerodha) ────────────
    def on_tick(self, combined, ce_ltp, pe_ltp, hm):
        self._check_control()
        self._hm = hm
        if ce_ltp is not None:
            self.marks[self.ce] = ce_ltp
        if pe_ltp is not None:
            self.marks[self.pe] = pe_ltp
        if self.ledger.open_shorts():
            breached, _ = self.guard.check_mtm(self.marks)
            if breached:
                self._flatten("MTM stop")
            elif self.guard.must_square_off(self._now()):
                self._flatten("time square-off")
        if self._trades_allowed:
            self.trigger.on_tick(combined, hm)
        self._write_tick(combined)

    def on_candle(self, ohlcv):
        o, h, l, c = (float(ohlcv["open"]), float(ohlcv["high"]),
                      float(ohlcv["low"]), float(ohlcv["close"]))
        vol = float(ohlcv.get("volume", 0) or 0)
        typ = (h + l + c) / 3
        self._cum_pv += typ * vol
        self._cum_vol += vol
        vwap = round(self._cum_pv / self._cum_vol, 2) if self._cum_vol > 0 else round(c, 2)
        if self._trades_allowed:
            self.trigger.on_candle_close({"time": ohlcv["time"], "open": o, "high": h,
                                          "low": l, "close": c, "vwap": vwap})
        mtm = self.ledger.mtm({k: v for k, v in self.marks.items() if v is not None})
        self._mtm_series.append({"t": ohlcv["time"], "rupees": mtm})
        self.persist()

    def seed(self, ohlcv_candles, split_fn):
        """Idempotent: rebuild guard/trigger/ledger/state from scratch (paper replay), so a
        mid-day restart re-establishes the current state without accumulating the counter."""
        self.ledger = Ledger()
        self.guard = RiskGuard(self.ledger, **self._gcfg)
        self.trigger = LiveTrigger(self._enter, self._exit, max_entries=self._gcfg["max_cycles"],
                                   entry_cutoff=self._gcfg["entry_cutoff"], square_off=self._gcfg["square_off"])
        self.cycles, self.events, self._open = [], [], None
        self._cum_pv = self._cum_vol = 0.0
        self._mtm_series, self._last_fill_time, self._oid = [], {}, 0
        self._seeding = True
        try:
            for cd in ohlcv_candles:
                ce, pe = split_fn(float(cd["low"]))
                self.on_tick(float(cd["low"]), ce, pe, cd["time"])
                self.on_candle(cd)
        finally:
            self._seeding = False

    # ── restart-safe recovery: make REAL Kotak fills the own-book truth ───────
    def reconcile_kotak(self):
        """Recover an open REAL Kotak position after a (re)start — the Kotak twin of
        controller.reconcile_broker. TAG-SCOPED to THIS mirror's `vwstk_kotak` fills, so it
        is immune to the user's manual Kotak trades and to broker netting.

        The seed() replay above rebuilds a PAPER approximation of the book. When we are
        live-armed that paper short is only a guess (wrong fill prices, and it could differ
        from what actually filled), and it would (a) fake the equity curve and (b) risk a
        false MTM-stop or a mis-sized cover. So in live mode we DROP the paper book and
        rebuild ledger + cycles + equity straight from the real fills; in paper mode we just
        fold in any real fills we didn't know about. No client -> nothing to reconcile.

        Mode is primed from the persisted KOTAK_{index} flag FIRST, so a restart while live
        takes the live branch at attach (before the first tick) — otherwise the default
        'paper' would fold the real fills onto the paper seed and double the short."""
        if not (self.kotak and self.kotak_syms):
            return
        try:                                            # prime mode from the persisted flag
            from live.control_flags import read_control
            m = read_control(f"KOTAK_{self.index}").get("mode")
            if m in ("paper", "live"):
                self.mode = m
        except Exception:
            pass
        ts_to_fy = {v["trading_symbol"]: fy for fy, v in self.kotak_syms.items()}
        try:
            fills = ke.strategy_fills(self.kotak, tag=TAG)
        except Exception as e:
            audit.log(self.index, "KOTAK_RECONCILE_FAIL", error=str(e))
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
            self.ledger = Ledger()                      # real fills are the ONLY truth in live
            self.guard.L = self.ledger                  # keep the guard's brakes on the live book
            for f in mine:
                self.ledger.record(Order(f["order_id"], f["fy"], f["side"], f["qty"], 0, "reconciled"))
                self.ledger.update_fill(f["order_id"], COMPLETE, f["qty"], f["avg_price"], f["fill_time"])
            self._rebuild_cycles_from_fills(mine)
        else:
            for f in mine:                              # paper: fold in only unknown real fills
                if f["order_id"] in self.ledger.orders:
                    continue
                self.ledger.record(Order(f["order_id"], f["fy"], f["side"], f["qty"], 0, "reconciled"))
                self.ledger.update_fill(f["order_id"], COMPLETE, f["qty"], f["avg_price"], f["fill_time"])

        ce_s, pe_s = self.ledger.open_short_real(self.ce), self.ledger.open_short_real(self.pe)
        audit.log(self.index, "KOTAK_RECONCILE", added=len(mine), ce_short=ce_s, pe_short=pe_s,
                  in_pos=self.trigger.in_pos, mode=self.mode)
        self.persist()

    def _rebuild_cycles_from_fills(self, fills: list):
        """Rebuild the displayed cycles + equity curve from REAL Kotak fills (time-ordered),
        pairing each SELL-pair (entry) with the BUY-pair that covers it (exit). Mirror of the
        Zerodha controller's method so the Live tab, after a restart, shows the true broker
        trades, real per-leg prices/times, real P&L, and a clean stepped equity curve."""
        cycles, cur, n = [], None, 0
        short = {self.ce: 0, self.pe: 0}
        for f in fills:
            sym, side, px, t, q = f["fy"], f["side"], f["avg_price"], f.get("fill_time"), f["qty"]
            if side == SELL:
                if short[self.ce] <= 0 and short[self.pe] <= 0:      # flat -> new cycle entry
                    n += 1
                    cur = {"cycle": n, "entry_time": t, "entry_ce": None, "entry_pe": None,
                           "entry_combined": None, "exit_time": None, "exit_ce": None,
                           "exit_pe": None, "exit_combined": None, "points": None, "pnl": None,
                           "trigger": None, "live": True, "reconciled": True}
                    cycles.append(cur)
                short[sym] += q
                if cur is not None:
                    cur["entry_ce" if sym == self.ce else "entry_pe"] = px
                    cur["entry_ce_time" if sym == self.ce else "entry_pe_time"] = t
                    if cur["entry_ce"] is not None and cur["entry_pe"] is not None:
                        cur["entry_combined"] = round(cur["entry_ce"] + cur["entry_pe"], 2)
            else:                                                     # BUY -> cover / exit
                short[sym] -= q
                if cur is not None:
                    cur["exit_ce" if sym == self.ce else "exit_pe"] = px
                    cur["exit_ce_time" if sym == self.ce else "exit_pe_time"] = t
                    cur["exit_time"] = t
                    if cur["exit_ce"] is not None and cur["exit_pe"] is not None:
                        cur["exit_combined"] = round(cur["exit_ce"] + cur["exit_pe"], 2)
                        cur["points"] = round((cur["entry_combined"] or 0) - cur["exit_combined"], 2)
                        cur["pnl"] = round(cur["points"] * self.qty, 2)
                        if short[self.ce] <= 0 and short[self.pe] <= 0:
                            cur = None                                # cycle fully closed
        self.cycles = cycles
        if cycles and cycles[-1]["exit_combined"] is None:            # last cycle still open
            self._open = cycles[-1]
            self.trigger.in_pos = True
        else:
            self._open = None
            self.trigger.in_pos = False
        series, cum = [], 0.0                                         # clean stepped equity curve
        for c in cycles:
            if c["entry_time"]:
                series.append({"t": c["entry_time"], "rupees": round(cum, 2)})
            if c["pnl"] is not None:
                cum += c["pnl"]
                series.append({"t": c["exit_time"], "rupees": round(cum, 2)})
        self._mtm_series = series

    # ── trigger callbacks ────────────────────────────────────────────────────
    def _enter(self, combined_trigger, cycle, reason):
        ok, why = self.guard.validate_entry(cycle, f"{self.date}-e{cycle}", self._now())
        if not ok:
            self.events.append({"t": self._hm, "type": "entry_blocked", "cycle": cycle, "why": why})
            return
        live = self.is_live_armed() and not self._seeding
        try:
            ce_fill = self._sell(self.ce, cycle)
            pe_fill = self._sell(self.pe, cycle)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"  [kotak] {self.index} ENTRY FAILED cyc{cycle}: {msg}", flush=True)
            self.events.append({"t": self._hm, "type": "entry_order_failed", "cycle": cycle, "error": msg})
            audit.log(self.index, "KOTAK_ORDER_FAILED", cyc=cycle, error=msg)
            naked = self.guard.check_naked(self.ce, self.pe)
            if naked:
                self._cover_naked(naked, cycle, "entry failed mid-leg")
            self.guard.kill(f"entry failed: {msg}")
            self.trigger.done = True
            self.persist()
            return
        if live and (ce_fill is None or pe_fill is None):
            self.events.append({"t": self._hm, "type": "entry_incomplete", "cycle": cycle,
                                "ce": ce_fill, "pe": pe_fill})
            audit.log(self.index, "KOTAK_ENTRY_INCOMPLETE", cyc=cycle, ce=ce_fill, pe=pe_fill)
            naked = self.guard.check_naked(self.ce, self.pe)
            if naked:
                self._cover_naked(naked, cycle, "entry leg would not fill")
            self.guard.kill("entry incomplete — a leg would not fill")
            self.trigger.done = True
            self.persist()
            return
        self.guard.mark_fired(f"{self.date}-e{cycle}")
        combined = round((ce_fill or 0) + (pe_fill or 0), 2)
        self._open = {"cycle": cycle, "entry_time": self._hm, "entry_combined": combined,
                      "entry_ce": ce_fill, "entry_pe": pe_fill, "trigger": combined_trigger,
                      "entry_ce_time": self._last_fill_time.get(self.ce),
                      "entry_pe_time": self._last_fill_time.get(self.pe), "live": live,
                      "exit_time": None, "exit_combined": None, "points": None, "pnl": None}
        self.cycles.append(self._open)
        self.events.append({"t": self._hm, "type": "entry", "cycle": cycle, "combined": combined,
                            "ce": ce_fill, "pe": pe_fill, "reason": reason})
        if live:
            audit.log(self.index, "KOTAK_ENTRY", cyc=cycle, combined=combined, ce=ce_fill,
                      pe=pe_fill, trigger=combined_trigger)
        self.persist()

    def _exit(self, combined_price, cycle, reason):
        ce_out = self._buy(self.ce, cycle)
        pe_out = self._buy(self.pe, cycle)
        combined = round((ce_out or 0) + (pe_out or 0), 2)
        if self._open:
            pts = round((self._open["entry_combined"] or 0) - combined, 2)
            self._open.update(exit_time=self._hm, exit_combined=combined, exit_trigger=combined_price,
                              exit_ce=ce_out, exit_pe=pe_out,
                              exit_ce_time=self._last_fill_time.get(self.ce),
                              exit_pe_time=self._last_fill_time.get(self.pe),
                              points=pts, pnl=round(pts * self.qty, 2))
            self._open = None
        self.events.append({"t": self._hm, "type": "exit", "cycle": cycle, "combined": combined,
                            "ce": ce_out, "pe": pe_out, "reason": reason})
        if self.is_live_armed() and not self._seeding:
            audit.log(self.index, "KOTAK_EXIT", cyc=cycle, combined=combined, ce=ce_out, pe=pe_out, reason=reason)
        self.persist()

    def _flatten(self, reason):
        open_now = dict(self.ledger.open_shorts())
        ce_out = self._buy(self.ce, self.guard.max_cycles, kind="square_off") if self.ce in open_now else None
        pe_out = self._buy(self.pe, self.guard.max_cycles, kind="square_off") if self.pe in open_now else None
        if self._open:
            combined = round((ce_out or 0) + (pe_out or 0), 2) if (ce_out is not None or pe_out is not None) \
                else round(sum(self.marks.get(s) or 0 for s in (self.ce, self.pe)), 2)
            pts = round((self._open["entry_combined"] or 0) - combined, 2)
            self._open.update(exit_time=self._hm, exit_combined=combined, exit_ce=ce_out,
                              exit_pe=pe_out, points=pts, pnl=round(pts * self.qty, 2))
            self._open = None
        self.trigger.done = True
        self.trigger.in_pos = False
        self.events.append({"t": self._hm, "type": "flatten", "reason": reason})
        if self.is_live_armed() and not self._seeding:
            audit.log(self.index, "KOTAK_SQUAREOFF", reason=reason,
                      real_covered=(ce_out is not None or pe_out is not None))
        self.persist()

    def _cover_naked(self, naked, cycle, reason):
        sym, units = naked
        self.events.append({"t": self._hm, "type": "naked_cover", "symbol": sym, "units": units, "reason": reason})
        self._buy(sym, cycle, kind="naked_cover", qty=units)

    # ── order placement: paper simulates; live goes to Kotak ─────────────────
    def _sell(self, sym, cycle, qty=None):
        return self._place(sym, SELL, cycle, "entry", qty)

    def _buy(self, sym, cycle, kind="exit", qty=None):
        return self._place(sym, BUY, cycle, kind, qty)

    def _place(self, sym, side, cycle, kind, qty=None):
        qty = qty or self.qty
        live = self.is_live_armed() and not self._seeding
        if live and side == BUY:                          # own-book gate: never a naked buy
            held = self.ledger.open_short_real(sym)
            if held <= 0:
                self.events.append({"t": self._hm, "type": "skip_buy_no_short", "symbol": sym, "kind": kind})
                return self.marks.get(sym)
            qty = min(qty, held)
        if live:
            return self._place_live(sym, side, cycle, kind, qty)
        # paper: simulated fill at the leg's current mark
        self._oid += 1
        oid = f"paper-k-{kind}-{self._oid}"      # MUST start 'paper' so open_short_real excludes it
        fill = self.marks.get(sym)
        ft = dt.datetime.now().strftime("%H:%M:%S")
        self.ledger.record(Order(oid, sym, side, qty, cycle, kind))
        self.ledger.update_fill(oid, COMPLETE, filled_qty=qty, avg_price=fill, fill_time=ft)
        self._last_fill_time[sym] = ft
        return fill

    def _place_live(self, sym, side, cycle, kind, qty):
        ks = self.kotak_syms.get(sym)
        if not (self.kotak and ks):
            raise RuntimeError(f"no Kotak client/contract for {sym}")
        price = ke.marketable_limit(self.marks.get(sym), side)
        oid = ke.place_limit(self.kotak, ks["trading_symbol"], ks["exchange_segment"],
                             side, qty, price, tag=TAG)
        self.ledger.record(Order(oid, sym, side, qty, cycle, kind))
        audit.log(self.index, "KOTAK_ORDER_PLACED", cyc=cycle, side=side,
                  sym=ks["trading_symbol"], qty=qty, oid=oid)
        fill, ft = self._poll_fill(oid, seconds=5.0)
        if fill is not None:
            ft = ft or dt.datetime.now().strftime("%H:%M:%S")
            self.ledger.update_fill(oid, COMPLETE, filled_qty=qty, avg_price=fill, fill_time=ft)
            self._last_fill_time[sym] = ft
            audit.log(self.index, "KOTAK_ORDER_COMPLETE", cyc=cycle, side=side,
                      sym=ks["trading_symbol"], avg=fill, oid=oid)
            return fill
        audit.log(self.index, "KOTAK_ORDER_NOFILL", cyc=cycle, side=side, sym=ks["trading_symbol"], oid=oid)
        return None

    def _poll_fill(self, oid, seconds=5.0):
        deadline = _time.monotonic() + seconds
        while _time.monotonic() < deadline:
            st = ke.order_status(self.kotak, oid)
            s = str(st.get("status") or "").lower()
            if st.get("filled_qty") and ("complete" in s or "traded" in s or s == "filled"):
                return st.get("avg_price"), st.get("fill_time")
            if "reject" in s or "cancel" in s:
                return None, None
            _time.sleep(0.7)
        return None, None

    # ── real-time tick snapshot + persisted state (own KOTAK files) ──────────
    def _write_tick(self, combined):
        now = _time.monotonic()
        if now - self._last_tick_write < 0.4:
            return
        self._last_tick_write = now
        try:
            mtm = self.ledger.mtm({k: v for k, v in self.marks.items() if v is not None})
            (STATE_DIR / f"{self.date}_{self.index}_KOTAK_TICK.json").write_text(json.dumps({
                "t": self._hm, "combined": round(combined, 2), "mtm": mtm,
                "ce": self.marks.get(self.ce), "pe": self.marks.get(self.pe),
                "armed": self.is_live_armed(),
                "updated": dt.datetime.now().strftime("%H:%M:%S")}))
        except Exception:
            pass

    def snapshot(self):
        realized = sum(c["pnl"] for c in self.cycles if c["pnl"] is not None)
        mtm = self.ledger.mtm({k: v for k, v in self.marks.items() if v is not None})
        return {"index": self.index, "date": self.date, "broker": "KOTAK", "mode": self.mode,
                "dte": self.dte, "armed": self.is_live_armed(), "trades_allowed": self._trades_allowed,
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
            (STATE_DIR / f"{self.date}_{self.index}_KOTAK.json").write_text(
                json.dumps(self.snapshot(), indent=2))
        except Exception:
            pass
