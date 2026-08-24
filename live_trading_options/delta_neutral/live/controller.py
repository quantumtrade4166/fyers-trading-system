"""
live/controller.py — the delta-neutral strangle state machine for ONE index.
============================================================================

Drives one index's day:

    09:30            enter a strangle at the target premium, both legs atomically,
                     each with a resting stop at the exchange
    every 15 min     inside a 1-minute window only, re-balance:
                     - both legs live and one is >= 2x the other -> replace the
                       smaller leg with a strike just below the open leg's premium
                     - one leg live (the other was stopped out) -> re-enter that
                       side by the same "just below" rule
                     - flat -> re-open a fresh strangle at the target premium,
                       UNLESS the position went flat by being stopped out on both
                       legs, which ends the day
    (0 DTE only)     the leg stop tightens on a schedule — 40 -> 30 at 12:00 ->
                     20 at 14:00 — each step arming only once every open leg is
                     far enough below the current stop for the new one to be
                     placeable at all
    15:14            cancel every stop, cover everything, done for the day

Three ways the day ends early: the max-loss limit, the KILL switch, and both legs
being stopped out. All of them stop new orders permanently for that session.

Two rules override everything else and are enforced on EVERY tick, not just at
decision points:

  1. NO SHORT WITHOUT A STOP. A leg is only OPEN once its stop is confirmed
     resting at the exchange. A leg that fills but cannot be protected is bought
     straight back. `Position.unprotected_legs()` must be empty at the end of
     every tick, and the snapshot publishes it so the UI can show the truth.

  2. NO INCOMPLETE STRANGLE AT ENTRY. If one leg of a fresh entry will not fill
     or will not take a stop, the other leg is covered immediately.

Single-legged running IS allowed — but only between a stop-out and the next
adjustment window, which is the behaviour the strategy asks for. It is surfaced
as a warning state rather than hidden.

Own book, never the broker's netted position: all sizing and P&L come from this
strategy's own `dnstrangle`-tagged fills, so the user's manual trades on the same
strikes can never confuse it.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import windows as W
from core.selector import (atm_strike, select_entry_leg, select_reentry_leg,
                           needs_adjustment, CE, PE)
from live.position import Position, Leg
from live.executor import Executor
from live.broker import audit_log, read_control

STATE_DIR = ROOT / "data" / "live_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)


class DNController:
    def __init__(self, index: str, date_str: str, expiry, dte: int, *,
                 params: dict, lot_size: int, kite=None, symbol_lookup=None):
        self.index, self.date, self.expiry, self.dte = index, date_str, expiry, dte
        self.params = params
        self.lot_size = lot_size
        self.lots = int(params.get("lots", 1))
        self.qty = self.lot_size * self.lots
        self.interval = params["strike_interval"][index]
        self.ratio = float(params.get("adjust_trigger_ratio", 2.0))
        self.max_loss = abs(float(params.get("max_loss", 1000)))
        self.max_fresh = int(params.get("max_fresh_entries", 3))
        self.wcfg = params.get("adjust_windows", {})
        self.entry_time = params.get("entry_time", "09:30")
        self.square_off = params.get("square_off", "15:14")

        # per-index/DTE entry rule (target premium + preferred band + stop level)
        rule = (params.get("entry", {}).get(index, {}) or {}).get(str(dte), {}) or {}
        self.target = rule.get("target")
        self.prefer_min, self.prefer_max = rule.get("prefer_min"), rule.get("prefer_max")
        self.trades_allowed = dte in (0, 1) and self.target is not None

        # `sl` is the level in force RIGHT NOW. On expiry day it tightens through
        # `sl_schedule` as the session runs on; every use of a stop level in this
        # class reads self.sl, so entries, re-entry selection and stop placement
        # all follow the schedule without any of them knowing it exists.
        self.sl_open = rule.get("sl")            # the level the day started on
        self.sl = self.sl_open
        self.sl_steps = [(s["from"], float(s["sl"]))
                         for s in (rule.get("sl_schedule") or [])]
        self.sl_arm_gap = float(params.get("sl_arm_gap", 10))
        self.sl_watchdog_gap = float(params.get("sl_watchdog_gap", 10))
        self.sl_step_i = 0                       # how many steps have taken effect
        self.sl_history: list[dict] = []
        self._sl_defer_logged = None

        # points between the SL trigger and its limit. Config may give one number
        # for everything or a per-index map (Sensex premiums are ~4x Nifty's, so
        # the same gap in POINTS is a very different gap in practice).
        buf = params.get("sl_limit_buffer", 2.0)
        self.sl_buffer = float(buf.get(index, 2.0) if isinstance(buf, dict) else buf)
        self.executor = Executor(index, expiry, tag=params.get("live_orders", {})
                                 .get("tag", "dnstrangle"), kite=kite, live=False,
                                 clock=self._hm, sl_buffer=self.sl_buffer)
        self._symbol_lookup = symbol_lookup      # (strike, opt_type) -> fyers symbol

        self.position = Position()
        self.mode = "paper"
        self.killed = False
        self.kill_reason = None
        self.done = False                        # trading finished for the day
        self.entered = False                     # the 09:30 entry has been attempted
        self.fresh_entries = 0
        self.done_windows: set[str] = set()
        self.events: list[dict] = []
        self.mtm_series: list[dict] = []
        self.chain: dict = {}
        self.spot = None
        self.atm = None
        self._now = None
        self._last_ctrl = 0.0
        self._last_tick_write = 0.0
        self._last_stop_poll = 0.0
        self._last_heartbeat = None

    # ── logging ──────────────────────────────────────────────────────────
    def _log(self, etype: str, **fields):
        """Record one action — in the snapshot AND in the append-only audit file.

        EVERYTHING is audited, paper included, tagged with the mode. The audit log
        is the only record that survives a crash (the snapshot is overwritten and
        stdout is buffered), and a paper day is exactly when you most want to be
        able to reconstruct what the strategy thought it was doing."""
        ev = {"t": self._hm(), "type": etype, **fields}
        self.events.append(ev)
        print(f"  [dn] {self.index} {etype}: "
              + " ".join(f"{k}={v}" for k, v in fields.items()), flush=True)
        audit_log(self.index, etype.upper(), mode=self.mode, **fields)

    def _hm(self) -> str:
        return (self._now or dt.datetime.now()).strftime("%H:%M:%S")

    # ── marks ────────────────────────────────────────────────────────────
    def _mark(self, leg) -> float | None:
        if leg is None:
            return None
        v = self.chain.get((leg.strike, leg.opt_type))
        return None if v is None else float(v)

    def marks(self) -> dict:
        out = {}
        for leg in (self.position.ce, self.position.pe):
            if leg is not None:
                m = self._mark(leg)
                if m is not None:
                    out[leg.symbol] = m
        return out

    # ── control channel (arm / kill / qty / max loss) ─────────────────────
    def _check_control(self):
        import time
        now = time.monotonic()
        if now - self._last_ctrl < 1.0:
            return
        self._last_ctrl = now
        try:
            c = read_control(self.index)
        except Exception:
            return
        # size + max-loss changes apply ONLY while flat, so an open position can
        # never be resized underneath itself
        if self.position.is_flat:
            q = c.get("qty")
            if isinstance(q, (int, float)) and q > 0 and q % self.lot_size == 0 \
                    and (q // self.lot_size) <= int(self.params.get("max_lots", 15)) \
                    and int(q) != self.qty:
                self.qty = int(q)
                self.lots = int(q) // self.lot_size
            m = c.get("mtm_stop")
            if isinstance(m, (int, float)) and m > 0:
                self.max_loss = abs(float(m))
        new_mode = c.get("mode")
        if new_mode in ("paper", "live") and new_mode != self.mode:
            self.mode = new_mode
            self.executor.set_live(new_mode == "live")
            audit_log(self.index, "ARM" if new_mode == "live" else "DISARM",
                         mode=new_mode, dte=self.dte, qty=self.qty,
                         max_loss=self.max_loss)
        elif new_mode in ("paper", "live"):
            self.executor.set_live(new_mode == "live")
        if c.get("kill") and not self.killed:
            audit_log(self.index, "KILL_PRESSED", live_legs=self.position.n_live)
            self._kill("kill switch")

    def _kill(self, reason: str):
        if not self.killed:
            self.killed = True
            self.kill_reason = reason
        if not self.position.is_flat:
            self._flatten(reason)
        self.done = True

    # ── restart recovery ─────────────────────────────────────────────────
    def reconcile_broker(self):
        """Rebuild today's position from THIS strategy's own broker orders.

        Called once at startup. Without it, an engine restarted mid-day begins
        flat, sees `is_flat` at the next adjustment window, and opens a SECOND
        strangle on top of the one still live at the broker. (That class of bug —
        a restart losing the real position — is what made the VWAP strangle's KILL
        fail to square off on 13-Aug.)

        TAG-SCOPED to `dnstrangle`, and built from our own order ids rather than
        the broker's netted position, so the user's manual trades on the same
        strikes cannot be mistaken for ours.

        Legs recovered here are matched to their still-resting stop orders, so a
        recovered position is correctly seen as PROTECTED and is not covered by
        the protection invariant.
        """
        if not self.executor.broker_ready:
            return
        from live.broker import kite_executor as kx
        kite, tag = self.executor.kite, self.executor.tag
        try:
            fills = kx.strategy_fills(kite, tag)
            stops = kx.resting_stops(kite, tag)
        except Exception as e:
            audit_log(self.index, "RECONCILE_FAIL", error=f"{type(e).__name__}: {e}")
            return

        # net short per contract, from our own fills only
        net: dict[str, int] = {}
        sells: dict[str, list] = {}
        for f in sorted(fills, key=lambda x: (x.get("fill_time") or "")):
            ts = f["tradingsymbol"]
            net[ts] = net.get(ts, 0) + (f["qty"] if f["side"] == "SELL" else -f["qty"])
            if f["side"] == "SELL":
                sells.setdefault(ts, []).append(f)
        if not fills:
            return

        self.entered = True             # the day's entry clearly already happened
        stop_by_sym = {s["tradingsymbol"]: s for s in stops}
        recovered = []
        for ts, qty in net.items():
            if qty <= 0:                                   # already covered
                continue
            c = kx.contract_for(kite, self.executor.exchange, ts)
            if c is None or c["opt_type"] not in (CE, PE):
                audit_log(self.index, "RECONCILE_SKIP", symbol=ts,
                          reason="contract not found in the instrument dump")
                continue
            side = c["opt_type"]
            last = sells[ts][-1]
            leg = Leg(side, c["strike"], ts, qty, exchange=self.executor.exchange,
                      reason="recovered from the broker after a restart")
            leg.mark_filled(last["order_id"], last["avg_price"], last.get("fill_time"))
            st = stop_by_sym.get(ts)
            if st:
                # recovered from a REAL resting order, so this one IS at the broker
                leg.mark_protected(st["order_id"], st.get("trigger_price") or self.sl,
                                   at_broker=True, time_str=self._hm())
            # no resting stop -> stays NAKED, and _enforce_protection replaces it
            self.position.set_leg(leg)
            recovered.append(f"{c['strike']}{side}@{last['avg_price']}"
                             f"{'' if st else ' (NO STOP)'}")

        audit_log(self.index, "RECONCILE", fills=len(fills), stops=len(stops),
                  recovered=", ".join(recovered) or "nothing open",
                  shape=("strangle" if self.position.is_complete
                         else "single leg" if self.position.is_single else "flat"))
        print(f"  [dn] {self.index} reconciled: {', '.join(recovered) or 'nothing open'}",
              flush=True)
        self.persist()

    # ── the tick ─────────────────────────────────────────────────────────
    def on_tick(self, chain: dict, spot: float, now: dt.datetime):
        """One market update. `chain` is {(strike, 'CE'|'PE'): ltp}."""
        self._now = now
        self.chain = chain or self.chain
        if spot:
            self.spot = float(spot)
            self.atm = atm_strike(self.spot, self.interval)
        self._check_control()

        self._check_sl_step(now)
        self._heartbeat(now)

        if not self.trades_allowed:
            self._write_tick()
            return

        self._detect_stops()
        self._stop_watchdog()
        self._enforce_protection()

        if not self.position.is_flat:
            mtm = self.position.mtm(self.marks())
            if mtm <= -self.max_loss:
                self._log("max_loss_hit", mtm=mtm, limit=-self.max_loss)
                self._kill(f"max loss hit ({mtm} <= -{self.max_loss})")
                self._write_tick()
                return

        if W.past_square_off(now, self.square_off):
            if not self.done:
                self._flatten("15:14 square-off")
                self.done = True
            self._write_tick()
            return

        if not (self.done or self.killed):
            if not self.entered and W.is_entry_time(now, self.entry_time):
                self.entered = True
                self._fresh_entry("09:30 entry")
            else:
                key = W.window_key(now, first=self.wcfg.get("first", "09:45"),
                                   last=self.wcfg.get("last", "15:00"),
                                   every_minutes=self.wcfg.get("every_minutes", 15),
                                   window_seconds=self.wcfg.get("window_seconds", 60))
                if key and key not in self.done_windows:
                    self.done_windows.add(key)
                    self._run_window(key)

        self._write_tick()

    # ── stop detection + continuous broker verification ──────────────────
    def _detect_stops(self):
        """Notice a leg whose stop has fired, AND keep confirming the stops that
        have not.

        PAPER simulates the exchange: the stop fills the moment the premium trades
        at or through the trigger. LIVE asks the broker — the exchange owns the
        stop, so its fill is the truth, not our tick stream.

        The verification half matters as much as the detection half. A stop can
        stop existing without ever firing: cancelled by hand in Kite, rejected
        late, or dropped in a broker-side event. Confirming it on every poll means
        a leg that has quietly lost its protection is demoted to NAKED within
        seconds and the protection invariant replaces it — instead of the terminal
        showing a shield for an order that is no longer there."""
        import time
        live = self.executor.is_live
        if not live:
            for side in (CE, PE):
                leg = self.position.leg(side)
                if leg is None or not leg.is_live or leg.sl_trigger is None:
                    continue
                m = self._mark(leg)
                if m is not None and m >= leg.sl_trigger:
                    leg.mark_stopped(max(m, leg.sl_trigger), self._hm())
                    self._log("stop_hit", side=side, strike=leg.strike,
                              trigger=leg.sl_trigger, fill=leg.exit_price, paper=True)
                    self._retire(side)
                    if self.position.is_flat:
                        self._stopped_flat()
                        return
            return

        if (time.monotonic() - self._last_stop_poll) < 2.0:
            return
        self._last_stop_poll = time.monotonic()
        from live.broker import kite_executor as kx
        try:
            book = kx.orders_by_id(self.executor.kite, self.executor.tag)
        except Exception as e:
            self._log("broker_poll_failed", error=f"{type(e).__name__}: {e}")
            return

        for side in (CE, PE):
            leg = self.position.leg(side)
            if leg is None or not leg.is_live or leg.sl_order_id is None:
                continue
            o = book.get(leg.sl_order_id)
            if o is None:                       # the stop is not in today's book at all
                if leg.sl_at_broker:
                    leg.mark_unprotected(self._hm())
                    self._log("stop_vanished", side=side, strike=leg.strike,
                              order=leg.sl_order_id,
                              note="not in the broker's order book — re-placing")
                continue
            if o["status"] == "COMPLETE":
                leg.mark_stopped(o.get("avg_price"), o.get("fill_time") or self._hm())
                self._log("stop_hit", side=side, strike=leg.strike,
                          trigger=leg.sl_trigger, limit=leg.sl_limit,
                          fill=o.get("avg_price"), order=leg.sl_order_id,
                          fill_time=o.get("fill_time"))
                self._retire(side)
                if self.position.is_flat:
                    self._stopped_flat()
                    return
            elif o["resting"]:
                leg.sl_at_broker = True         # re-confirmed this poll
                leg.sl_verified = True
                leg.sl_checked = self._hm()
            else:                                # CANCELLED / REJECTED / anything else
                leg.mark_unprotected(self._hm())
                self._log("stop_lost", side=side, strike=leg.strike,
                          order=leg.sl_order_id, status=o["status"],
                          note="stop no longer resting — re-placing")

    def _retire(self, side: str):
        self.position.retire(side)
        self._record_equity()

    def _stopped_flat(self):
        """Both legs have now been stopped out — the day is over.

        Being stopped on ONE leg is routine: the position runs single-legged until
        the next adjustment window re-enters that side. Being stopped on BOTH means
        the market has gone through the strategy in both directions, and re-opening
        into that is how a bad day becomes a much worse one. So a stop-out that
        leaves the position flat ends trading; no fresh strangle, no re-entry.

        Only stop-outs do this. An adjustment that leaves the position flat, or an
        entry that failed on a technicality, still get another go at the next
        window — those are not the market stopping us out.
        """
        self._log("stopped_out_flat",
                  reason="both legs stopped out — no re-entry, done for the day",
                  realized=self.position.realized())
        self._kill("both legs stopped out — done for the day")

    # ── the skipped-stop watchdog ────────────────────────────────────────
    def _stop_watchdog(self):
        """Cover a leg ourselves when its exchange stop has clearly been skipped.

        The stop is an SL with a limit (trigger 40, limit 42) because Zerodha will
        not accept SL-M on options. In a violent move the market can print straight
        through 42 without filling it — the order then rests, unfilled, while the
        leg keeps losing, and every screen still shows it protected. That is the
        one hole a resting stop cannot cover by itself.

        So: premium at or beyond trigger + `sl_watchdog_gap` while the leg is still
        live means the stop did not do its job. Cancel it and buy the leg back now,
        at the market, rather than waiting for the max-loss limit to notice.

        Runs on the TICK, not on the broker poll — the whole point is speed. It is
        also why it must run AFTER stop detection: a stop that genuinely filled is
        recognised first, so this only ever sees the skipped case.

        In paper the simulated stop always fills exactly at the trigger, so this
        cannot fire. It is a live-only safety net.
        """
        for side in (CE, PE):
            leg = self.position.leg(side)
            if leg is None or not leg.is_live or not leg.sl_trigger:
                continue
            m = self._mark(leg)
            if m is None or m < leg.sl_trigger + self.sl_watchdog_gap:
                continue
            self._log("stop_skipped", side=side, strike=leg.strike,
                      trigger=leg.sl_trigger, limit=leg.sl_limit, premium=m,
                      past_by=round(m - leg.sl_trigger, 2),
                      sl_order=leg.sl_order_id,
                      note="exchange stop did not fill — covering at market")
            # booked as a stop loss, because that is what it is — the exchange just
            # failed to execute it, so it must not read as a normal exit
            self._cover(leg, f"stop loss (skipped at {leg.sl_trigger} — force covered)")
            self._retire(side)
            if self.position.is_flat:
                self._stopped_flat()
                return

    # ── the protection invariant ─────────────────────────────────────────
    def _enforce_protection(self):
        """No short may exist without a confirmed stop. Runs every tick as the
        backstop for any path that leaves a leg unprotected — try once more to
        place the stop, and if that still fails, cover the leg."""
        for leg in list(self.position.unprotected_legs()):
            trigger = leg.sl_trigger or self.sl
            oid, ok, at_broker, limit = self.executor.place_stop(leg, trigger)
            if ok:
                leg.mark_protected(oid, trigger, at_broker, limit, self._hm())
                self._log("stop_placed", side=leg.opt_type, strike=leg.strike,
                          trigger=trigger, limit=limit, order=oid,
                          at_broker=at_broker, recovered=True)
                continue
            self._log("unprotected_cover", side=leg.opt_type, strike=leg.strike,
                      reason="stop could not be placed")
            self._cover(leg, "no stop — covered for safety")
            self._retire(leg.opt_type)

    # ── opening and closing single legs ──────────────────────────────────
    def _build_leg(self, side: str, cand: dict) -> Leg | None:
        """Turn a selection into a placeable leg.

        The tradingsymbol comes from the chain, which built it from Kite's own
        instrument dump — the same dump the order layer resolves against, so the
        contract we priced is by construction the contract we trade."""
        ts = self._symbol_lookup(cand["strike"], side) if self._symbol_lookup else None
        if not ts:
            self._log("leg_build_failed", side=side, strike=cand["strike"],
                      reason="strike not in the Kite chain")
            return None
        leg = Leg(side, cand["strike"], ts, self.qty, exchange=self.executor.exchange,
                  otm_level=cand.get("otm_level"), reason=cand.get("why"))
        return leg

    def _open_leg(self, leg: Leg, mark: float) -> bool:
        """Sell one leg and protect it. Returns True only when the leg ends up
        SHORT AND PROTECTED — any other outcome is unwound here, so the caller
        never has to reason about a half-open leg."""
        fill = self.executor.sell(leg, mark)
        if not fill.ok:
            self._log("entry_nofill", side=leg.opt_type, strike=leg.strike,
                      order=fill.order_id)
            return False
        leg.mark_filled(fill.order_id, fill.price, fill.time)
        oid, ok, at_broker, limit = self.executor.place_stop(leg, self.sl)
        if not ok:
            self._log("stop_failed", side=leg.opt_type, strike=leg.strike,
                      trigger=self.sl, limit=limit, order=oid)
            self._cover(leg, "stop rejected — leg unwound")
            return False
        leg.mark_protected(oid, self.sl, at_broker, limit, self._hm())
        self._log("leg_open", side=leg.opt_type, strike=leg.strike,
                  entry=leg.entry_price, sl_trigger=self.sl, sl_limit=limit,
                  sl_at_broker=at_broker, otm=leg.otm_level,
                  qty=leg.qty, order=fill.order_id, sl_order=oid, why=leg.reason)
        return True

    def _cover(self, leg: Leg, reason: str) -> float | None:
        """Cancel a leg's stop, then buy it back. The cancel MUST come first — a
        stop left resting after the leg is closed would fire later and open a new,
        unwanted long position."""
        if not self.executor.cancel_stop(leg):
            self._log("stop_cancel_failed", side=leg.opt_type, strike=leg.strike,
                      order=leg.sl_order_id)
        mark = self._mark(leg) or leg.entry_price
        fill = self.executor.buy(leg, mark, kind="exit")
        price = fill.price if fill.ok else mark
        leg.mark_closed(price, reason, fill.order_id, fill.time)
        self._log("leg_closed", side=leg.opt_type, strike=leg.strike,
                  exit=price, reason=reason, pnl=leg.pnl(), order=fill.order_id)
        return price

    # ── the three window actions ─────────────────────────────────────────
    def _run_window(self, key: str):
        pos = self.position
        if pos.is_complete:
            ce_m, pe_m = self._mark(pos.ce), self._mark(pos.pe)
            trig, small = needs_adjustment(ce_m, pe_m, self.ratio)
            if not trig:
                self._log("window_checked", window=key, ce=ce_m, pe=pe_m,
                          action="balanced — no adjustment")
                return
            self._adjust(key, small)
        elif pos.is_single:
            self._reenter_missing(key)
        else:
            self._fresh_entry(f"window {key} — re-open after flat")

    def _adjust(self, key: str, small_side: str):
        """Replace the smaller leg with one just below the open leg's premium.

        The replacement strike is validated BEFORE anything is closed. If no strike
        qualifies, the window is skipped whole and the existing position is left
        exactly as it is — closing the small leg first and only then discovering
        there is nothing to re-enter would strand the position single-legged for
        no reason."""
        pos = self.position
        open_side = PE if small_side == CE else CE
        open_leg = pos.leg(open_side)
        open_prem = self._mark(open_leg)
        small_leg = pos.leg(small_side)
        if open_prem is None:
            self._log("window_skipped", window=key, reason="no mark for the open leg")
            return

        cand = select_reentry_leg(self.chain, self.atm, self.interval, small_side,
                                  below_premium=open_prem, ratio=self.ratio, sl=self.sl)
        if cand is None:
            self._log("window_skipped", window=key, side=small_side,
                      open_leg=f"{open_leg.strike}{open_side}@{open_prem}",
                      reason="no strike just below the open leg — waiting for next window")
            return

        new_leg = self._build_leg(small_side, cand)
        if new_leg is None:
            self._log("window_skipped", window=key, reason="could not build the new leg")
            return

        self._log("adjust_triggered", window=key, replace=small_side,
                  ce=self._mark(pos.ce), pe=self._mark(pos.pe), ratio=self.ratio,
                  from_strike=small_leg.strike, to_strike=cand["strike"],
                  new_premium=cand["premium"], sl_gated=cand.get("sl_gated"))

        self._cover(small_leg, f"adjustment {key} — 2x rule")
        self._retire(small_side)
        if self._open_leg(new_leg, cand["premium"]):
            self.position.set_leg(new_leg)
        else:
            self._log("adjust_incomplete", window=key, side=small_side,
                      note="replacement leg would not open — single-legged until next window")
        self._record_equity()
        self.persist()

    def _reenter_missing(self, key: str):
        """Re-open the side that was stopped out, by the same "just below" rule."""
        pos = self.position
        side = pos.missing_side()
        open_leg = pos.leg(PE if side == CE else CE)
        open_prem = self._mark(open_leg)
        if open_prem is None:
            self._log("window_skipped", window=key, reason="no mark for the open leg")
            return
        cand = select_reentry_leg(self.chain, self.atm, self.interval, side,
                                  below_premium=open_prem, ratio=self.ratio, sl=self.sl)
        if cand is None:
            self._log("window_skipped", window=key, side=side,
                      open_leg=f"{open_leg.strike}{open_leg.opt_type}@{open_prem}",
                      reason="no strike just below the open leg — still single-legged")
            return
        leg = self._build_leg(side, cand)
        if leg is None:
            return
        self._log("reentry", window=key, side=side, strike=cand["strike"],
                  premium=cand["premium"], below=open_prem,
                  sl_gated=cand.get("sl_gated"))
        if self._open_leg(leg, cand["premium"]):
            self.position.set_leg(leg)
        self._record_equity()
        self.persist()

    def _fresh_entry(self, reason: str):
        """Open a complete strangle at the target premium — both legs or neither."""
        if self.fresh_entries >= self.max_fresh:
            self._log("entry_blocked", reason=f"max {self.max_fresh} fresh entries reached")
            return
        if self.atm is None or not self.chain:
            self._log("entry_blocked", reason="no chain/spot yet")
            return
        self.fresh_entries += 1

        picks = {}
        for side in (CE, PE):
            c = select_entry_leg(self.chain, self.atm, self.interval, side,
                                 target=self.target, prefer_min=self.prefer_min,
                                 prefer_max=self.prefer_max)
            if c is None:
                self._log("entry_blocked", side=side, reason="no strike found in the chain")
                return
            picks[side] = c

        self._log("entry_start", reason=reason, spot=self.spot, atm=self.atm,
                  target=self.target, sl=self.sl, qty=self.qty,
                  ce=f"{picks[CE]['strike']}@{picks[CE]['premium']}",
                  pe=f"{picks[PE]['strike']}@{picks[PE]['premium']}")

        legs = {}
        for side in (CE, PE):
            leg = self._build_leg(side, picks[side])
            if leg is None:
                self._unwind(legs, "leg could not be built")
                return
            if not self._open_leg(leg, picks[side]["premium"]):
                # ATOMIC ENTRY: one leg failed -> immediately close whatever opened
                self._unwind(legs, f"{side} leg failed — no incomplete strangles")
                return
            legs[side] = leg

        for side, leg in legs.items():
            self.position.set_leg(leg)
        self._log("entry_complete",
                  ce=f"{legs[CE].strike}@{legs[CE].entry_price}",
                  pe=f"{legs[PE].strike}@{legs[PE].entry_price}",
                  combined=round((legs[CE].entry_price or 0) + (legs[PE].entry_price or 0), 2))
        self._record_equity()
        self.persist()

    def _unwind(self, legs: dict, reason: str):
        """Close every leg that opened during a failed entry. This is what makes
        the entry atomic."""
        for side, leg in legs.items():
            if leg.is_live:
                self._cover(leg, f"entry unwound — {reason}")
                self.position.set_leg(leg)
                self._retire(side)
        self._log("entry_aborted", reason=reason)
        self.persist()

    # ── end of day / kill ────────────────────────────────────────────────
    def _flatten(self, reason: str):
        for side in (CE, PE):
            leg = self.position.leg(side)
            if leg is not None and leg.is_live:
                self._cover(leg, reason)
                self._retire(side)
        self._log("flatten", reason=reason, realized=self.position.realized())
        self._record_equity()
        self.persist()

    # ── expiry-day stop tightening ───────────────────────────────────────
    def _check_sl_step(self, now: dt.datetime):
        """Tighten the leg stop on schedule — but only when it is actually placeable.

        A step becomes DUE at its clock time and ARMS only once EVERY open leg is
        trading at least `sl_arm_gap` below the level currently in force. That
        condition exists because a buy-stop must sit ABOVE the market: tightening
        onto a leg that has already run past the new level is rejected by the
        broker, and forcing it through would just be an instant exit at whatever
        the clock happened to catch.

        Both legs move together or neither does. A due-but-unarmed step is not
        skipped — it stays pending and fires the moment the condition is met, which
        may be seconds or an hour later.
        """
        if not self.sl_steps or self.sl_step_i >= len(self.sl_steps):
            return
        at, new_sl = self.sl_steps[self.sl_step_i]
        if now.time() < W._t(at):
            return
        if new_sl >= self.sl:                    # not a tightening — nothing to do
            self.sl_step_i += 1
            return

        legs = self.position.live_legs()
        need = self.sl - self.sl_arm_gap         # every leg must be at/below this
        blockers = []
        for leg in legs:
            m = self._mark(leg)
            if m is None or m > need:
                blockers.append(f"{leg.strike}{leg.opt_type}@{m}")
        if blockers:
            # log the deferral ONCE per (step, reason) — this runs every tick
            key = (at, tuple(blockers))
            if self._sl_defer_logged != key:
                self._sl_defer_logged = key
                self._log("sl_step_deferred", due=at, from_sl=self.sl, to_sl=new_sl,
                          need_at_or_below=need, waiting_on=", ".join(blockers))
            return

        old = self.sl
        moved, failed = [], []
        for leg in legs:
            ok, limit = self.executor.modify_stop(leg, new_sl)
            if ok:
                leg.sl_trigger, leg.sl_limit, leg.sl_checked = new_sl, limit, self._hm()
                moved.append(f"{leg.strike}{leg.opt_type}")
            else:
                # the modify was refused; the OLD stop is still in the book, so the
                # leg stays protected. Demote it so the protection invariant re-places
                # it at the new level rather than silently leaving it wide.
                leg.mark_unprotected(self._hm())
                failed.append(f"{leg.strike}{leg.opt_type}")

        self.sl = new_sl
        self.sl_step_i += 1
        self._sl_defer_logged = None
        self.sl_history.append({"t": self._hm(), "at": at, "from": old, "to": new_sl,
                                "legs_moved": moved, "legs_failed": failed})
        self._log("sl_tightened", due=at, from_sl=old, to_sl=new_sl,
                  legs_moved=", ".join(moved) or "none open",
                  legs_failed=", ".join(failed) or None,
                  note="new legs from here are born with this stop")

    # ── periodic status heartbeat ────────────────────────────────────────
    def _heartbeat(self, now: dt.datetime):
        """Every 15 minutes, append a full status line to the audit log.

        Not driven by events — it fires whether or not anything happened, which is
        the point: an event log tells you what the strategy DID, and this tells you
        what it BELIEVED at fixed intervals. When something goes wrong the gap
        between the two is usually where the bug is.

        Each line carries, per live leg: strike, entry, current premium, distance
        to its stop, and whether the broker has CONFIRMED that stop is resting.
        """
        slot = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        key = slot.strftime("%H:%M")
        if key == self._last_heartbeat:
            return
        self._last_heartbeat = key

        legs = []
        for side in (CE, PE):
            leg = self.position.leg(side)
            if leg is None or not leg.is_live:
                legs.append(f"{side}:none")
                continue
            m = self._mark(leg)
            if self.executor.is_live:
                sl = (f"SL_AT_BROKER({leg.sl_trigger}/{leg.sl_limit})"
                      if leg.sl_at_broker else "SL_MISSING!")
            else:
                sl = f"SL_PAPER({leg.sl_trigger})"
            dist = None if (m is None or not leg.sl_trigger) else round(leg.sl_trigger - m, 2)
            legs.append(f"{side}:{leg.strike}@{leg.entry_price}"
                        f" now={m} to_sl={dist} {sl} qty={leg.qty}"
                        f" confirmed={leg.sl_checked or '-'}")

        shape = ("strangle" if self.position.is_complete
                 else "single-leg" if self.position.is_single else "flat")
        pending = (f"{self.sl_steps[self.sl_step_i][0]}->{self.sl_steps[self.sl_step_i][1]}"
                   if self.sl_step_i < len(self.sl_steps) else None)
        audit_log(self.index, "STATUS", mode=self.mode, at=key, shape=shape,
                  sl_now=self.sl, sl_next=pending,
                  spot=self.spot, atm=self.atm, dte=self.dte,
                  legs=" | ".join(legs),
                  realized=self.position.realized(),
                  mtm=self.position.mtm(self.marks()),
                  max_loss=self.max_loss,
                  unprotected=len(self.position.unprotected_legs()),
                  windows_done=len(self.done_windows),
                  entries=f"{self.fresh_entries}/{self.max_fresh}",
                  killed=self.killed or None, done=self.done or None)

    # ── state published to the dashboard ─────────────────────────────────
    def _record_equity(self):
        self.mtm_series.append({"t": self._hm()[:5],
                                "rupees": self.position.mtm(self.marks())})

    def _write_tick(self):
        """Small, frequent snapshot for the terminal's live P&L + chain (~0.4s)."""
        import time
        now = time.monotonic()
        if now - self._last_tick_write < 0.4:
            return
        self._last_tick_write = now
        try:
            m = self.marks()
            secs = W.seconds_to_next_window(
                self._now or dt.datetime.now(),
                first=self.wcfg.get("first", "09:45"), last=self.wcfg.get("last", "15:00"),
                every_minutes=self.wcfg.get("every_minutes", 15))
            (STATE_DIR / f"{self.date}_{self.index}_TICK.json").write_text(json.dumps({
                "t": self._hm(), "spot": self.spot, "atm": self.atm,
                "mtm": self.position.mtm(m),
                "realized": self.position.realized(),
                "ce": self._mark(self.position.ce), "pe": self._mark(self.position.pe),
                "n_live": self.position.n_live, "single": self.position.is_single,
                "armed": self.executor.is_live, "next_window_secs": secs,
                "chain": {f"{k[0]}{k[1]}": v for k, v in (self.chain or {}).items()},
                "updated": dt.datetime.now().strftime("%H:%M:%S"),
            }))
        except Exception:
            pass

    def snapshot(self) -> dict:
        m = self.marks()
        now = self._now or dt.datetime.now()
        return {
            "index": self.index, "date": self.date, "dte": self.dte,
            "expiry": str(self.expiry), "mode": self.mode,
            "armed": self.executor.is_live, "broker_ready": self.executor.broker_ready,
            "trades_allowed": self.trades_allowed, "killed": self.killed,
            "kill_reason": self.kill_reason, "done": self.done,
            "target": self.target, "sl": self.sl, "ratio": self.ratio,
            "sl_open": self.sl_open, "sl_history": self.sl_history,
            "sl_steps": [{"at": a, "sl": v} for a, v in self.sl_steps],
            "sl_step_i": self.sl_step_i, "sl_arm_gap": self.sl_arm_gap,
            "qty": self.qty, "lots": self.lots, "lot_size": self.lot_size,
            "max_loss": self.max_loss,
            "spot": self.spot, "atm": self.atm,
            "position": self.position.to_dict(m),
            "entered": self.entered, "fresh_entries": self.fresh_entries,
            "max_fresh_entries": self.max_fresh,
            "windows_done": sorted(self.done_windows),
            "next_window_secs": W.seconds_to_next_window(
                now, first=self.wcfg.get("first", "09:45"),
                last=self.wcfg.get("last", "15:00"),
                every_minutes=self.wcfg.get("every_minutes", 15)),
            "in_window": W.window_key(
                now, first=self.wcfg.get("first", "09:45"),
                last=self.wcfg.get("last", "15:00"),
                every_minutes=self.wcfg.get("every_minutes", 15),
                window_seconds=self.wcfg.get("window_seconds", 60)),
            "events": self.events, "mtm_series": self.mtm_series,
            "updated": dt.datetime.now().strftime("%H:%M:%S"),
        }

    def persist(self):
        (STATE_DIR / f"{self.date}_{self.index}_DN.json").write_text(
            json.dumps(self.snapshot(), indent=2))
