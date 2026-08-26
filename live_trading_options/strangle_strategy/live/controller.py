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
from live import audit

STATE_DIR = ROOT / "data" / "live_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def _t(s: str) -> dt.time:
    return dt.datetime.strptime(s, "%H:%M").time()


class LiveController:
    def __init__(self, index: str, date_str: str, ce_sym: str, pe_sym: str, dte,
                 *, lot_size: int, lots: int, max_cycles: int, mtm_stop: float,
                 entry_cutoff: str, square_off: str, mode: str = "paper", kite=None,
                 kite_syms: dict | None = None, allow_live: bool = False):
        self.index, self.date = index, date_str
        self.ce, self.pe, self.dte = ce_sym, pe_sym, dte
        self.lot_size, self.lots = lot_size, lots
        self.qty = lot_size * lots
        self.mode = mode                       # 'paper' | 'live' (from toggle/control)
        self.allow_live = allow_live           # HARD config gate: real orders need this
        self._last_ctrl = 0.0                  # throttle for reading the control file
        self.kite = kite
        self.kite_syms = kite_syms or {}       # {fyers_sym: {tradingsymbol, exchange}} for live
        # config kept so seed() can rebuild guard/trigger from scratch (idempotent)
        self._gcfg = dict(max_cycles=max_cycles, mtm_stop=mtm_stop,
                          entry_cutoff=entry_cutoff, square_off=square_off)
        self._seeding = False                  # True only during seed() replay
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
        self._cum_pv = 0.0                     # running VWAP: Σ(typical × volume)
        self._cum_vol = 0.0                    #               Σ(volume), from 9:15
        self._mtm_series = []                  # [{t, rupees}] intraday equity curve
        self._last_flatten_try = 0.0           # throttle for the keep-trying flatten
        self._last_fill_time = {}              # sym -> broker fill time (HH:MM:SS) of the last fill
        self._trades_allowed = dte in (0, 1)   # strategy trades ONLY DTE 0/1; else chart-only

    # ── inputs from the tick engine ───────────────────────────────────────
    def _check_control(self):
        """Read the dashboard's control flags (throttled to ~1s): obey the KILL
        switch (flatten + stop) and the Paper/Live toggle."""
        import time
        now = time.monotonic()
        if now - self._last_ctrl < 1.0:
            return
        self._last_ctrl = now
        try:
            from live.control_flags import read_control
            c = read_control(self.index)      # PER-INDEX arm switch (NIFTY vs SENSEX)
        except Exception:
            return
        # Size + MTM-stop overrides from the dashboard. Applied ONLY while FLAT so a
        # live change can never resize an open position mid-trade (the arming UI also
        # locks the fields once armed/in a position — this is the backend guard for it).
        if not self.ledger.open_shorts():
            q = c.get("qty")
            if isinstance(q, (int, float)) and q > 0 and q % self.lot_size == 0 \
                    and (q // self.lot_size) <= 15 and int(q) != self.qty:
                self.qty = int(q)
                self.lots = int(q) // self.lot_size
            m = c.get("mtm_stop")
            if isinstance(m, (int, float)) and m > 0:
                self.guard.mtm_stop = abs(float(m))
        new_mode = c.get("mode")
        if new_mode in ("paper", "live"):
            if new_mode != self.mode:            # arm/disarm flip — audit it (with size + stop)
                audit.log(self.index, "ARM" if new_mode == "live" else "DISARM",
                          mode=new_mode, dte=self.dte,
                          qty=self.qty, lots=self.lots, mtm_stop=self.guard.mtm_stop)
            self.mode = new_mode
        if c.get("kill") and not self.guard.killed:
            audit.log(self.index, "KILL_PRESSED", open_shorts=len(self.ledger.open_shorts()),
                      real_short_ce=self.ledger.open_short_real(self.ce),
                      real_short_pe=self.ledger.open_short_real(self.pe))
            self.guard.kill("kill switch")
            if self.ledger.open_shorts():
                self._flatten("kill switch")
            else:
                self.trigger.done = True

    def is_live_armed(self) -> bool:
        """Real orders fire when the runtime toggle says 'live'. The whole live layer
        is gated by config live_orders.enabled (the controller isn't even created
        otherwise), and the UI toggle carries a strong confirm — so this single
        RUNTIME flag (read from the control file each tick) is the deliberate arm.
        Runtime by design, so it never depends on config-vs-restart timing (that was
        the allow_live bug)."""
        return self.mode == "live"

    def on_tick(self, combined: float, ce_ltp: float, pe_ltp: float, hm: str):
        self._check_control()
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
            elif self.guard.killed:
                # Killed, yet still short: a previous flatten did not fill. Keep
                # trying. Without this the retry depended on MTM STAYING breached —
                # if premiums eased back the leg just sat there, unprotected and
                # unattended, until the 15:14 square-off.
                import time as _t
                if _t.monotonic() - self._last_flatten_try >= 2.0:
                    self._last_flatten_try = _t.monotonic()
                    self._flatten("still short after kill — retrying")
            elif self.guard.must_square_off(self._now()):
                # TICK-driven time square-off: fire the instant the clock reaches square_off
                # (e.g. 15:14) instead of waiting for the 5-min candle CLOSE, which lands ~5
                # min late (the 15:14/15:15 candle closes at ~15:19-15:20). _flatten already
                # stops the trigger for the day, so the candle-close path can't double-fire.
                self._flatten("time square-off")
        if self._trades_allowed:
            self.trigger.on_tick(combined, hm)
        self._write_tick(combined)             # real-time P&L / price for the Live tab

    def _write_tick(self, combined: float):
        """Publish a tiny live-tick snapshot (throttled ~0.4s) so the dashboard can show
        real-time P&L + price + a moving current candle — like a broker terminal. Kept
        separate + small so it's cheap to write every few ticks. Never raises."""
        import time as _t
        now = _t.monotonic()
        if now - getattr(self, "_last_tick_write", 0.0) < 0.4:
            return
        self._last_tick_write = now
        try:
            mtm = self.ledger.mtm({k: v for k, v in self.marks.items() if v is not None})
            (STATE_DIR / f"{self.date}_{self.index}_TICK.json").write_text(json.dumps({
                "t": self._hm, "combined": round(combined, 2), "mtm": mtm,
                "ce": self.marks.get(self.ce), "pe": self.marks.get(self.pe),
                "armed": self.is_live_armed(),
                "updated": dt.datetime.now().strftime("%H:%M:%S")}))
        except Exception:
            pass

    def on_candle(self, ohlcv: dict):
        """Feed a finished 5-min OHLCV candle (no VWAP needed). The controller keeps
        its OWN running VWAP (typical-price, cumulative from 9:15) so it's independent
        of the V2 archive — then drives the strategy. This is what the tick-engine tap
        will call on each candle close."""
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
        # intraday equity point (realized + unrealized, own book) for the Live MTM chart
        mtm = self.ledger.mtm({k: v for k, v in self.marks.items() if v is not None})
        self._mtm_series.append({"t": ohlcv["time"], "rupees": mtm})
        self.persist()

    def seed(self, ohlcv_candles: list, split_fn):
        """Reconstruct VWAP + strategy state from the morning's candles when the
        controller starts mid-day (or after a restart). Replays each candle with a
        low-tick so any entries/exits that already happened are re-booked. Fills are
        approximate here (no historical per-leg ticks) — only the *current* state
        needs to be right so the live ticks from now are handled correctly.

        IDEMPOTENT: rebuilds guard/trigger/ledger/state from scratch every call, so a
        re-seed (engine restart, WS-stall relaunch, or a second strike-selection) can
        NEVER accumulate the entry counter — the old bug that inflated cycles to 5 and
        blocked every entry. The replay runs as PAPER (self._seeding) so reconstructing
        past fills never sends a real Kite order."""
        self.ledger = Ledger()
        self.guard = RiskGuard(self.ledger, **self._gcfg)
        self.trigger = LiveTrigger(self._enter, self._exit,
                                   max_entries=self._gcfg["max_cycles"],
                                   entry_cutoff=self._gcfg["entry_cutoff"],
                                   square_off=self._gcfg["square_off"])
        self.cycles, self.events, self._open = [], [], None
        self._cum_pv = self._cum_vol = 0.0
        self._mtm_series = []
        self._last_fill_time = {}
        self._oid = 0
        self._seeding = True
        try:
            for cd in ohlcv_candles:
                ce, pe = split_fn(float(cd["low"]))
                self.on_tick(float(cd["low"]), ce, pe, cd["time"])
                self.on_candle(cd)
        finally:
            self._seeding = False

    def reconcile_broker(self):
        """Make the broker's own tagged fills the single source of truth after a (re)start.
        TAG-SCOPED: only THIS strategy's `vwstrangle` orders — immune to the user's manual
        trades and to broker netting.

        When LIVE-armed the seed-replay's PAPER orders are only an approximation, and a
        timed-out entry leaves a phantom paper short that would (a) show a fake rising
        equity curve, (b) hide the real trade behind the live-only filter, and (c) risk a
        false MTM-stop. So in live mode we DROP the paper book entirely and rebuild the
        ledger + displayed cycles + equity curve straight from the real fills. In paper
        mode we just fold in any real fills we didn't already know about."""
        if not (self.kite and self.kite_syms):
            return
        from live import kite_executor as kx
        ts_to_fy = {v["tradingsymbol"]: fy for fy, v in self.kite_syms.items()}
        try:
            fills = kx.strategy_fills(self.kite)
        except Exception as e:
            audit.log(self.index, "RECONCILE_FAIL", error=str(e))
            return
        mine = [{**f, "fy": ts_to_fy[f["tradingsymbol"]]}
                for f in fills if ts_to_fy.get(f["tradingsymbol"]) in (self.ce, self.pe)]
        mine.sort(key=lambda f: (f.get("fill_time") or ""))

        if self.is_live_armed():
            self.ledger = Ledger()                 # real fills are the ONLY truth in live
            for f in mine:
                self.ledger.record(Order(f["order_id"], f["fy"], f["side"], f["qty"], 0, "reconciled"))
                self.ledger.update_fill(f["order_id"], COMPLETE, f["qty"], f["avg_price"], f["fill_time"])
            self._rebuild_cycles_from_fills(mine)
        else:
            for f in mine:                         # paper: fold in only unknown real fills
                if f["order_id"] in self.ledger.orders:
                    continue
                self.ledger.record(Order(f["order_id"], f["fy"], f["side"], f["qty"], 0, "reconciled"))
                self.ledger.update_fill(f["order_id"], COMPLETE, f["qty"], f["avg_price"], f["fill_time"])

        ce_s, pe_s = self.ledger.open_short_real(self.ce), self.ledger.open_short_real(self.pe)
        audit.log(self.index, "RECONCILE", added=len(mine), ce_short=ce_s, pe_short=pe_s,
                  in_pos=self.trigger.in_pos)
        self.persist()

    def _rebuild_cycles_from_fills(self, fills: list):
        """Rebuild the displayed cycles + equity curve from REAL broker fills (time-
        ordered), pairing each SELL-pair (entry) with the BUY-pair that covers it (exit).
        This is what the Live tab shows, so after a restart it reflects the true broker
        trades, real per-leg fill prices/times, real P&L, and a clean stepped equity
        curve — never the paper reconstruction."""
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
        # clean stepped equity curve from realized cycle exits (live ticks extend it forward)
        series, cum = [], 0.0
        for c in cycles:
            if c["entry_time"]:
                series.append({"t": c["entry_time"], "rupees": round(cum, 2)})
            if c["pnl"] is not None:
                cum += c["pnl"]
                series.append({"t": c["exit_time"], "rupees": round(cum, 2)})
        self._mtm_series = series

    # ── trigger callbacks ────────────────────────────────────────────────
    def _now(self) -> dt.datetime:
        return dt.datetime.combine(dt.date.fromisoformat(self.date), _t(self._hm))

    def _enter(self, combined_trigger: float, cycle: int, reason: str):
        ok, why = self.guard.validate_entry(cycle, f"{self.date}-e{cycle}", self._now())
        if not ok:
            self.events.append({"t": self._hm, "type": "entry_blocked", "cycle": cycle, "why": why})
            return
        live = self.is_live_armed() and not self._seeding
        try:
            if live:
                # fire BOTH shorts together + retry a laggard (smallest naked window)
                ce_fill, pe_fill = self._sell_pair_live(cycle)
            else:
                ce_fill = self._sell(self.ce, cycle)
                pe_fill = self._sell(self.pe, cycle)
        except Exception as e:
            # A broker rejection must fail SAFE, not crash the trigger (which used to
            # re-fire and spin the counter). Log the exact error unbuffered, cover any
            # leg that DID fill, and stop trading for the day.
            msg = f"{type(e).__name__}: {e}"
            print(f"  [live] {self.index} ENTRY ORDER FAILED cyc{cycle}: {msg}", flush=True)
            self.events.append({"t": self._hm, "type": "entry_order_failed",
                                "cycle": cycle, "error": msg})
            audit.log(self.index, "ORDER_FAILED", cyc=cycle, error=msg)
            naked = self.guard.check_naked(self.ce, self.pe)
            if naked:
                self._cover_naked(naked, cycle, "entry failed mid-leg")
            self.guard.kill(f"entry order failed: {msg}")
            self.trigger.done = True
            self.persist()
            return
        # LIVE: a leg that never completed (even after retries) = failed entry. Cover the
        # leg that DID fill so we're never left naked, then stop for the day.
        if live and (ce_fill is None or pe_fill is None):
            print(f"  [live] {self.index} ENTRY INCOMPLETE cyc{cycle}: ce={ce_fill} pe={pe_fill} "
                  f"— covering filled leg + stopping", flush=True)
            self.events.append({"t": self._hm, "type": "entry_incomplete",
                                "cycle": cycle, "ce": ce_fill, "pe": pe_fill})
            audit.log(self.index, "ENTRY_INCOMPLETE", cyc=cycle, ce=ce_fill, pe=pe_fill)
            naked = self.guard.check_naked(self.ce, self.pe)
            if naked:
                self._cover_naked(naked, cycle, "entry leg would not fill")
            self.guard.kill("entry incomplete — a leg would not fill")
            self.trigger.done = True
            self.persist()
            return
        naked = self.guard.check_naked(self.ce, self.pe)   # qty-imbalance safety
        if naked:
            self._cover_naked(naked, cycle, "entry leg failed")
        self.guard.mark_fired(f"{self.date}-e{cycle}")
        combined = round((ce_fill or 0) + (pe_fill or 0), 2)
        self._open = {"cycle": cycle, "entry_time": self._hm, "entry_combined": combined,
                      "entry_ce": ce_fill, "entry_pe": pe_fill, "trigger": combined_trigger,
                      "entry_ce_time": self._last_fill_time.get(self.ce),
                      "entry_pe_time": self._last_fill_time.get(self.pe), "live": live,
                      "exit_time": None, "exit_combined": None, "points": None, "pnl": None}
        self.cycles.append(self._open)
        self.events.append({"t": self._hm, "type": "entry", "cycle": cycle,
                            "combined": combined, "ce": ce_fill, "pe": pe_fill, "reason": reason})
        if live:
            audit.log(self.index, "ENTRY", cyc=cycle, combined=combined, ce=ce_fill, pe=pe_fill,
                      ce_time=self._last_fill_time.get(self.ce),
                      pe_time=self._last_fill_time.get(self.pe), trigger=combined_trigger)
        self.persist()

    def _exit(self, combined_price: float, cycle: int, reason: str):
        ce_out = self._buy(self.ce, cycle)
        pe_out = self._buy(self.pe, cycle)
        combined = round((ce_out or 0) + (pe_out or 0), 2)
        pts = None
        if self._open:
            pts = round((self._open["entry_combined"] or 0) - combined, 2)
            self._open.update(exit_time=self._hm, exit_combined=combined,
                              exit_trigger=combined_price, exit_ce=ce_out, exit_pe=pe_out,
                              exit_ce_time=self._last_fill_time.get(self.ce),
                              exit_pe_time=self._last_fill_time.get(self.pe),
                              points=pts, pnl=round(pts * self.qty, 2))
            self._open = None
        self.events.append({"t": self._hm, "type": "exit", "cycle": cycle,
                            "combined": combined, "ce": ce_out, "pe": pe_out, "reason": reason})
        if self.is_live_armed() and not self._seeding:
            audit.log(self.index, "EXIT", cyc=cycle, combined=combined, ce=ce_out, pe=pe_out,
                      pts=pts, pnl=(round(pts * self.qty, 2) if pts is not None else None),
                      ce_time=self._last_fill_time.get(self.ce),
                      pe_time=self._last_fill_time.get(self.pe), reason=reason)
        self.persist()

    # ── flatten (kill / square-off): buy back exactly the OWN open shorts ──
    def _flatten(self, reason: str):
        open_now = dict(self.ledger.open_shorts())
        ce_out = self._buy(self.ce, self.guard.max_cycles, kind="square_off") if self.ce in open_now else None
        pe_out = self._buy(self.pe, self.guard.max_cycles, kind="square_off") if self.pe in open_now else None
        if self._open:
            combined = round((ce_out or 0) + (pe_out or 0), 2) if (ce_out is not None or pe_out is not None) \
                else round(sum(self.marks.get(s) or 0 for s in (self.ce, self.pe)), 2)
            pts = round((self._open["entry_combined"] or 0) - combined, 2)
            self._open.update(exit_time=self._hm, exit_combined=combined,
                              exit_trigger=None, exit_ce=ce_out, exit_pe=pe_out,
                              exit_ce_time=self._last_fill_time.get(self.ce),
                              exit_pe_time=self._last_fill_time.get(self.pe),
                              points=pts, pnl=round(pts * self.qty, 2))
            self._open = None
        # a kill/flatten ends trading for the day — stop the trigger so it can't fire
        # phantom entries/exits after the position is already closed (desync fix).
        self.trigger.done = True
        self.trigger.in_pos = False
        self.events.append({"t": self._hm, "type": "flatten", "reason": reason})
        if self.is_live_armed() and not self._seeding:
            audit.log(self.index, "SQUAREOFF", reason=reason, ce=ce_out, pe=pe_out,
                      real_covered=(ce_out is not None or pe_out is not None))
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
        # A real order fires ONLY when armed AND not reconstructing past state (seed).
        live = self.is_live_armed() and not self._seeding
        if live and side == BUY:
            # OWN-BOOK: never buy back more than THIS strategy actually holds short.
            # Guards the mid-day case where the reconstructed position is a phantom
            # (entry happened before arming, so no real short exists) — a live exit
            # must NOT place a naked buy.
            held = self.ledger.open_short_real(sym)
            if held <= 0:
                self.events.append({"t": self._hm, "type": "skip_buy_no_short",
                                    "symbol": sym, "kind": kind})
                return self.marks.get(sym)
            qty = min(qty, held)
        if live:
            return self._place_live(sym, side, cycle, kind, qty)
        # paper: simulated fill at the current LTP of this leg
        self._oid += 1
        oid = f"paper-{kind}-{self._oid}"
        fill = self.marks.get(sym)
        ft = dt.datetime.now().strftime("%H:%M:%S")     # paper "fill time" (legs ~instant)
        o = Order(oid, sym, side, qty, cycle, kind)
        self.ledger.record(o)
        self.ledger.update_fill(oid, COMPLETE, filled_qty=qty, avg_price=fill, fill_time=ft)
        self._last_fill_time[sym] = ft
        return fill

    # marketable-limit buffer: price THROUGH the touch so it fills at the best bid/ask
    # like a market order (Zerodha rejects market orders on options). Only a worst-case
    # cap — the actual fill is at the market. 30% clears any normal option spread.
    _MKT_BUF = 0.30
    # escalating marketable buffers for retrying a laggard leg (fill harder each try)
    _RETRY_BUFS = (0.30, 0.60, 0.90)

    def _limit_price(self, sym: str, side: str, buf: float = None,
                     cushion_ticks: int = 2) -> float:
        """Marketable limit for one leg, priced off the ACTUAL order book when it
        can be read, and off a multiple of the mark only as a fallback.

        A multiple of the mark is a guess: too tight and the order never fills,
        too wide and it can sweep a thin book and fill somewhere terrible. The
        depth says what is really on offer and in what size, so we reach exactly
        as far as our quantity requires plus a few ticks."""
        buf = self._MKT_BUF if buf is None else buf
        ref = self.marks.get(sym) or 0.0
        if ref <= 0:                                  # no mark yet → uncapped marketable
            fallback = 0.05 if side == SELL else 100000.0
        else:
            fallback = ref * (1 - buf) if side == SELL else ref * (1 + buf)
        if not (self.is_live_armed() and self.kite and (self.kite_syms or {}).get(sym)):
            return fallback                           # paper, or no resolved contract
        try:
            from live import kite_executor as kx
            ks = self.kite_syms[sym]
            return kx.marketable_price(self.kite, ks["exchange"], ks["tradingsymbol"],
                                       side, self.qty, fallback=fallback,
                                       cushion_ticks=cushion_ticks) or fallback
        except Exception:
            return fallback

    # ── smart two-leg entry: fire BOTH shorts together, then retry a laggard ──
    def _fire_leg(self, kx, sym: str, side: str, cycle: int, kind: str, buf: float) -> dict:
        """Place ONE marketable-limit leg immediately (no wait) and record it."""
        ks = self.kite_syms[sym]
        oid = kx.place_limit_verified(self.kite, ks["tradingsymbol"], ks["exchange"],
                                      side, self.qty, self._limit_price(sym, side, buf))
        self.ledger.record(Order(oid, sym, side, self.qty, cycle, kind))
        return {"oid": oid, "fill": None}

    def _poll_legs(self, kx, legs: dict, seconds: float):
        """Poll each still-open leg for up to `seconds`; set leg['fill'] on COMPLETE."""
        import time
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if all(legs[s]["fill"] is not None for s in legs):
                return
            for s in legs:
                if legs[s]["fill"] is not None:
                    continue
                st = kx.order_status(self.kite, legs[s]["oid"])
                self.ledger.update_fill(legs[s]["oid"], st["status"],
                                        st.get("filled_qty"), st.get("avg_price"), st.get("fill_time"))
                if st["status"] == "COMPLETE":
                    legs[s]["fill"] = st.get("avg_price")
                    self._last_fill_time[s] = st.get("fill_time")
            time.sleep(0.4)

    def _recancel_leg(self, kx, legs: dict, sym: str) -> bool:
        """Cancel a stuck leg so it can be re-priced. Returns True if it actually FILLED
        during the cancel race (keep it), False if safely cancelled (safe to re-place)."""
        oid = legs[sym]["oid"]
        try:
            kx.cancel(self.kite, oid)
        except Exception:
            pass
        st = kx.order_status(self.kite, oid)
        self.ledger.update_fill(oid, st["status"], st.get("filled_qty"), st.get("avg_price"), st.get("fill_time"))
        if st["status"] == "COMPLETE":
            legs[sym]["fill"] = st.get("avg_price")
            self._last_fill_time[sym] = st.get("fill_time")
            return True
        return False

    def _sell_pair_live(self, cycle: int):
        """Fire BOTH short legs at once (smallest naked window), poll both, and retry a
        laggard at a MORE aggressive price up to twice. Returns (ce_fill, pe_fill); a leg
        that still won't fill is None (caller covers the other + stops)."""
        from live import kite_executor as kx
        legs = {sym: self._fire_leg(kx, sym, SELL, cycle, "entry", self._RETRY_BUFS[0])
                for sym in (self.ce, self.pe)}
        for attempt in range(len(self._RETRY_BUFS)):
            self._poll_legs(kx, legs, seconds=4 if attempt == 0 else 2)
            stuck = [s for s in legs if legs[s]["fill"] is None]
            if not stuck:
                break
            if attempt < len(self._RETRY_BUFS) - 1:
                for sym in stuck:
                    if self._recancel_leg(kx, legs, sym):        # filled during cancel race
                        continue
                    self.events.append({"t": self._hm, "type": "leg_retry",
                                        "symbol": sym, "attempt": attempt + 1})
                    print(f"  [live] {self.index} leg {sym} not filled — retry "
                          f"{attempt + 1} more aggressively", flush=True)
                    legs[sym] = self._fire_leg(kx, sym, SELL, cycle, "entry",
                                               self._RETRY_BUFS[attempt + 1])
        # cancel any leg that never filled, so nothing rests in the book
        for sym in legs:
            if legs[sym]["fill"] is None:
                try:
                    kx.cancel(self.kite, legs[sym]["oid"])
                    self.ledger.update_fill(legs[sym]["oid"], "CANCELLED")
                except Exception:
                    pass
        return legs[self.ce]["fill"], legs[self.pe]["fill"]

    def _place_live(self, sym: str, side: str, cycle: int, kind: str, qty: int) -> float:
        """LIVE: fire a real marketable-LIMIT order via the executor, poll the fill,
        book it. Kite symbol comes from the pre-resolved kite_syms map (never
        hand-built). Raises on a broker error — _enter catches it and fails safe."""
        from live import kite_executor as kx
        import time
        ks = self.kite_syms[sym]
        price = self._limit_price(sym, side)
        oid = kx.place_limit_verified(self.kite, ks["tradingsymbol"], ks["exchange"], side, qty, price)
        audit.log(self.index, "ORDER_PLACED", cyc=cycle, side=side, sym=ks["tradingsymbol"],
                  qty=qty, limit=round(price, 2), kind=kind, oid=oid)
        o = Order(oid, sym, side, qty, cycle, kind)
        self.ledger.record(o)
        fill = None
        for _ in range(10):                    # poll up to ~5s for the fill
            st = kx.order_status(self.kite, oid)
            self.ledger.update_fill(oid, st["status"], st.get("filled_qty"), st.get("avg_price"), st.get("fill_time"))
            if st["status"] in ("COMPLETE", "REJECTED", "CANCELLED"):
                fill = st.get("avg_price")
                if st["status"] == "COMPLETE":
                    self._last_fill_time[sym] = st.get("fill_time")
                audit.log(self.index, "ORDER_" + st["status"], cyc=cycle, side=side,
                          sym=ks["tradingsymbol"], avg=fill, fill_time=st.get("fill_time"), oid=oid)
                break
            time.sleep(0.5)
        else:
            # never filled in the poll window — cancel the resting remainder so we don't
            # leave a live order in the book, then report no fill (naked handling upstream).
            try:
                kx.cancel(self.kite, oid)
                self.ledger.update_fill(oid, "CANCELLED")
                audit.log(self.index, "ORDER_NOFILL", cyc=cycle, side=side,
                          sym=ks["tradingsymbol"], oid=oid, note="cancelled after 5s")
                print(f"  [live] {self.index} {side} {sym} not filled in 5s — cancelled", flush=True)
            except Exception as e:
                print(f"  [live] {self.index} cancel after no-fill failed: {e}", flush=True)
        return fill

    # ── state for the Live tab ────────────────────────────────────────────
    def snapshot(self) -> dict:
        realized = sum(c["pnl"] for c in self.cycles if c["pnl"] is not None)
        mtm = self.ledger.mtm({k: v for k, v in self.marks.items() if v is not None})
        return {"index": self.index, "date": self.date, "mode": self.mode, "dte": self.dte,
                "armed": self.is_live_armed(), "trades_allowed": self._trades_allowed,
                "broker_ready": bool(self.kite and self.kite_syms),
                "ce_symbol": self.ce, "pe_symbol": self.pe, "qty": self.qty,
                "killed": self.guard.killed, "kill_reason": self.guard.kill_reason,
                "open": self._open, "cycles": self.cycles, "events": self.events,
                "orders": [o.to_dict() for o in self.ledger.orders.values()],
                "marks": self.marks, "realized_pnl": round(realized, 2), "mtm_pnl": mtm,
                "mtm_series": self._mtm_series,
                "reconcile": self.guard.check_reconcile(),
                "updated": dt.datetime.now().strftime("%H:%M:%S")}

    def persist(self):
        (STATE_DIR / f"{self.date}_{self.index}_LIVE.json").write_text(
            json.dumps(self.snapshot(), indent=2))
