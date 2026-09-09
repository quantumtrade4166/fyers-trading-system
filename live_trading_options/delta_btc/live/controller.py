"""
live/controller.py — the delta-neutral strangle state machine for ONE profile.
==============================================================================

A faithful port of the NSE delta-neutral controller. The strategy is unchanged —
that is the point of the exercise — and what varies is the CLOCK, which now comes
from a `SessionProfile` instead of being hardcoded to an exchange session.

One cycle:

    entry time       open a strangle at the target premium, both legs atomically,
                     each with a stop armed before the leg counts as OPEN
    every 15 min     inside a 60-second window only, re-balance:
                     - both legs live and one is >= 2x the other -> replace the
                       smaller leg with a strike just below the open leg's premium
                     - one leg live (the other stopped out) -> re-enter that side
                       by the same "just below" rule
                     - flat -> re-open a fresh strangle, UNLESS the cycle ended
    square-off       cancel every stop, cover everything, cycle over

WHAT THE PROFILE CHANGES
Only the clock and the temperament. `ends_on_both_stopped` and `ends_on_max_loss`
are True for A and B — a double stop-out or the loss limit ends the cycle, which
is the NSE behaviour. For C they are False: it flattens and re-enters at the next
window, capped by `max_fresh_entries`. That single pair of flags is the whole
difference between "one strangle per expiry" and "always short", and keeping it
to flags is what stops C from drifting into being a different strategy.

TWO RULES OVERRIDE EVERYTHING, enforced every poll and not only at decisions:

  1. NO SHORT WITHOUT A STOP. A leg is OPEN only once its stop is armed. A leg
     that fills but cannot be protected is bought straight back.
     `Position.unprotected_legs()` must be empty at the end of every poll.

  2. NO INCOMPLETE STRANGLE AT ENTRY. If one leg of a fresh entry will not fill,
     the other is covered immediately.

Single-legged running IS allowed — but only between a stop-out and the next
window, which is the behaviour the strategy asks for. It is surfaced as a warning
state, never hidden.

PAPER ONLY. Every "order" is simulated in `executor.py`. The failure paths are
kept anyway — a paper cover with no book on the other side genuinely does fail,
and a month of paper that quietly pretended otherwise would teach us nothing
about the month of live that follows.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import datetime as dt
from pathlib import Path

from core.selector import (select_entry_leg, select_reentry_leg,
                           needs_adjustment, CE, PE)
from live.position import Position, Leg
from live.executor import Executor

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "data" / "live_state"
RESULTS = ROOT / "data" / "results"
LOGS = ROOT / "logs"
for _d in (STATE_DIR, RESULTS, LOGS):
    _d.mkdir(parents=True, exist_ok=True)


class BTCController:
    def __init__(self, profile, params: dict, *, chain=None, out_dir=None):
        """`out_dir` redirects every file this controller writes — the cycle log,
        the equity samples, the audit trail and the snapshot.

        It exists so the offline harness cannot write into the real month's
        results. A test run that appended fake cycles to `data/results/cycles.jsonl`
        would corrupt the very dataset the experiment is being judged on, and it
        would do it silently.
        """
        self.profile = profile
        self.name = profile.name
        self.params = params
        self.chain_obj = chain
        base = Path(out_dir) if out_dir else None
        self.results_dir = (base / "results") if base else RESULTS
        self.logs_dir = (base / "logs") if base else LOGS
        self.state_dir = (base / "live_state") if base else STATE_DIR
        for d in (self.results_dir, self.logs_dir, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.ratio = float(params.get("adjust_trigger_ratio", 2.0))
        self.max_loss = abs(float((params.get("max_loss_usd") or {})
                                  .get(profile.name, 100)))
        self.contracts = profile.contracts
        self.target = profile.target
        self.sl = profile.sl

        self.fees_cfg = params.get("fees", {})
        self.cross = bool((params.get("slippage") or {}).get("cross_the_spread", True))

        # ── per-cycle state ──────────────────────────────────────────────
        self.cycle: str | None = None
        self.expiry: str | None = None
        self.position = Position()
        self.entered = False
        self.done = False           # cycle finished cleanly
        self.killed = False         # cycle stopped early (max loss / both stopped)
        self.kill_reason = None
        self.fresh_entries = 0
        self.done_windows: set = set()
        self.stuck: dict = {}       # side -> reason a cover would not fill
        # A cycle whose first entry did NOT happen at the profile's own entry time
        # is not a representative cycle of that profile — it is what happens when
        # the engine is started (or restarted) mid-cycle, and it gives a 23.6h
        # profile an 8h holding period, which is very nearly profile A. Recorded
        # rather than blocked, so the comparison can exclude it from the headline
        # numbers while still showing it happened.
        self.first_entry_at = None
        self.late_start = False
        # Set by the engine when the push feed is up. Optional by design — see _mark.
        self.feed = None

        # ── across the month ─────────────────────────────────────────────
        self.cycles_done: list = []
        self.equity: list = []
        self._now = None
        self._last_equity_min = None

        self.executor = Executor(profile.name, chain, fees=self.fees_cfg,
                                 cross_the_spread=self.cross, clock=self._hm)

    # ── small helpers ────────────────────────────────────────────────────
    def _hm(self) -> str:
        return self._now.strftime("%H:%M:%S") if self._now else ""

    def _stamp(self) -> str:
        return self._now.strftime("%Y-%m-%d %H:%M:%S") if self._now else ""

    def _log(self, event: str, **fields):
        """One flushed line per event. Never raises — a logging failure must not
        be able to take down a controller holding a position."""
        rec = {"ts": self._stamp(), "profile": self.name, "cycle": self.cycle,
               "event": event, **fields}
        try:
            day = (self._now or dt.datetime.now()).date().isoformat()
            with open(self.logs_dir / f"{day}_btc_audit.log", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            pass

    def _mark(self, leg) -> float | None:
        """This leg's current premium.

        The PUSH feed wins when it has a fresh price for this exact contract: the
        REST chain is up to `poll_seconds` old, and a stop noticed five seconds
        late is real money on BTC. The feed returns None the moment its price goes
        stale (or if the socket never connected at all), and then this falls back
        to the chain — so the strategy is correct with the socket permanently
        down, just slower to react.
        """
        if leg is None:
            return None
        feed = getattr(self, "feed", None)
        if feed is not None:
            m = feed.mark(leg.symbol)
            if m is not None:
                return m
        if self.chain_obj is None:
            return None
        return self.chain_obj.mark.get((leg.strike, leg.opt_type))

    def marks(self) -> dict:
        """{symbol: mark} for every leg the book knows about."""
        out = {}
        for leg in (self.position.ce, self.position.pe):
            if leg is not None:
                out[leg.symbol] = self._mark(leg)
        return out

    @property
    def mtm(self) -> float:
        return self.position.mtm(self.marks())

    # ── the cycle boundary ───────────────────────────────────────────────
    def _roll_cycle(self, key: str | None, now: dt.datetime):
        """Close the books on the cycle that just ended and start the next.

        Called whenever the profile's cycle key changes — including to None, which
        is the flat gap between cycles. Recording happens on the way OUT of a
        cycle, so a cycle that ends because the engine was restarted mid-gap is
        still written exactly once.

        THE SQUARE-OFF HAPPENS HERE, and it has to. A cycle's bounds are the
        half-open interval [start, end), so at exactly the square-off time the
        profile is already OUT of its cycle and this method is what runs. An
        earlier version left the flatten to a `past_square_off` branch further
        down the poll — which never got to run, because the book had already been
        replaced by the time it was reached. Every leg still open at the boundary
        was dropped with its P&L unrealized, and the cycle was recorded as if it
        had closed flat. Flatten FIRST, then record, then reset.
        """
        if self.cycle is not None and not self.position.is_flat:
            self._flatten("square-off")
            if self.stuck:
                # In paper this needs a strike with no book at all on the ask; in
                # live it would mean genuinely still being short past the
                # square-off, which is the loudest thing this system can say.
                self._log("cycle_end_stuck", stuck=sorted(self.stuck),
                          note="STILL SHORT at the cycle boundary — position "
                               "carried into the record, not silently dropped")

        if self.cycle is not None and (self.entered or self.position.history):
            rec = {
                "profile": self.name, "cycle": self.cycle, "expiry": self.expiry,
                "ended": now.strftime("%Y-%m-%d %H:%M:%S"),
                # `realized` is NET — it is the number every comparison quotes.
                # `gross` is carried beside it so the cost of trading is legible
                # per cycle rather than only in aggregate: A, B and C re-leg
                # different numbers of times, so the gap between these two columns
                # is itself one of the things the month is measuring.
                "realized": self.position.realized(),
                "gross": round(sum(l.gross_pnl() or 0 for l in self.position.history), 4),
                "fees": self.position.fees_paid(),
                "legs": len(self.position.history),
                "adjustments": sum(1 for l in self.position.history
                                   if (l.exit_reason or "").startswith("adjustment")),
                "stopped_legs": sum(1 for l in self.position.history
                                    if l.status == "STOPPED"),
                "fresh_entries": self.fresh_entries,
                "ended_early": bool(self.killed),
                "kill_reason": self.kill_reason,
                "left_stuck": sorted(self.stuck) or None,
                # Every exit reason the cycle went through, in order. The book
                # itself is reset immediately below, so this is the only record
                # of HOW each leg left — and it is what the comparison tool reads
                # to tell an adjustment apart from a stop-out apart from a
                # square-off without having to re-parse the audit log.
                "exit_reasons": [l.exit_reason for l in self.position.history],
                "late_start": self.late_start,
                "first_entry": (self.first_entry_at.strftime("%Y-%m-%d %H:%M")
                                if self.first_entry_at else None),
                "hours_held": (round((now - self.first_entry_at).total_seconds() / 3600, 2)
                               if self.first_entry_at else 0.0),
                "exposure_hours": self.profile.exposure_hours,
                "target": self.target, "sl": self.sl, "contracts": self.contracts,
            }
            self.cycles_done.append(rec)
            self._log("cycle_closed", **rec)
            try:
                with open(self.results_dir / "cycles.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
            except Exception:
                pass

        self.cycle = key
        self.position = Position()
        self.entered = False
        self.done = False
        self.killed = False
        self.kill_reason = None
        self.fresh_entries = 0
        self.done_windows = set()
        self.stuck = {}
        self.expiry = None
        self.first_entry_at = None
        self.late_start = False
        if key:
            self._log("cycle_open", entry=str(self.profile.entry_time),
                      square_off=str(self.profile.square_off),
                      target=self.target, sl=self.sl, contracts=self.contracts,
                      max_loss=self.max_loss)

    def _end_cycle(self, reason: str):
        """Stop trading for the rest of THIS cycle."""
        if self.killed:
            return
        self.killed = True
        self.kill_reason = reason
        self._log("cycle_ended_early", reason=reason, mtm=self.mtm)
        if not self.position.is_flat:
            self._flatten(reason)

    # ── the poll ─────────────────────────────────────────────────────────
    def on_tick(self, chain_obj, now: dt.datetime):
        """One market update. `chain_obj` is the LiveChain for this cycle's expiry."""
        self._now = now
        if chain_obj is not None:
            self.chain_obj = chain_obj
            self.executor.chain = chain_obj

        key = self.profile.cycle_key(now)
        if key != self.cycle:
            self._roll_cycle(key, now)

        # Before anything else: a leg whose cover did not fill is still short with
        # its stop already cancelled. Nothing matters more, and no early return
        # below is allowed to skip it.
        self._retry_stuck()
        self._record_equity(now)

        if self.chain_obj is None or not self.chain_obj.is_ready():
            self._write_state()
            return

        self._detect_stops()
        self._enforce_protection()

        # max loss
        if not self.position.is_flat and not self.killed:
            m = self.mtm
            if m <= -self.max_loss:
                self._log("max_loss_hit", mtm=m, limit=-self.max_loss)
                if self.profile.ends_on_max_loss:
                    self._end_cycle(f"max loss hit ({m} <= -{self.max_loss})")
                else:
                    # C: flatten and stand down until the next window, but the
                    # cycle stays alive so it can go short again.
                    self._flatten(f"max loss hit ({m}) — continuous: re-arm next window")
                self._write_state()
                return

        if self.profile.past_square_off(now):
            if not self.done and not self.stuck and not self.position.is_flat:
                self._flatten("square-off")
            self.done = self.position.is_flat and not self.stuck
            self._write_state()
            return

        if not self.killed:
            if not self.entered and self.profile.is_entry_time(now):
                self.entered = True
                self.expiry = getattr(self.chain_obj, "expiry", None)
                self._fresh_entry("cycle entry")
            else:
                wkey = self.profile.window_key(now)
                if wkey and wkey not in self.done_windows:
                    self.done_windows.add(wkey)
                    self._run_window(wkey)

        self._write_state()

    # ── stops ────────────────────────────────────────────────────────────
    def _detect_stops(self):
        """Fire any leg whose simulated stop has been reached.

        Paper simulates the exchange: the stop fills when the MARK reaches the
        trigger, which is what Delta's own `stop_trigger_method: mark_price` would
        do live. The fill is booked at the WORSE of the trigger and the current
        mark — a gap through the level fills where the market actually is, not at
        the level we wished for, and pretending otherwise would flatter every
        stop-out in the month.
        """
        for side in (CE, PE):
            leg = self.position.leg(side)
            if leg is None or not leg.is_live or leg.sl_trigger is None:
                continue
            m = self._mark(leg)
            if m is None or not self.executor.stop_triggered(leg, m):
                continue
            fill = max(m, leg.sl_trigger)
            fee = self.executor.fee_for(fill, leg.contracts, leg.contract_value,
                                        self.chain_obj.spot)
            leg.mark_stopped(fill, self._hm(), fee=fee)
            self._log("stop_hit", side=side, strike=leg.strike,
                      trigger=leg.sl_trigger, fill=fill, gapped=round(m - leg.sl_trigger, 2),
                      pnl=leg.pnl())
            self._retire(side)
            if self.position.is_flat:
                self._stopped_flat()
                return

    def _retire(self, side: str):
        self.position.retire(side)

    def _stopped_flat(self):
        """Both legs stopped out.

        Being stopped on ONE leg is routine — the position runs single-legged until
        the next window re-enters that side. Being stopped on BOTH means the market
        has gone through the strategy in both directions, and re-opening into that
        is how a bad cycle becomes a much worse one.

        A and B stop for the cycle. C does not, by design — that is the behaviour
        the month is measuring, and it is the single most likely way C loses.
        """
        self._log("stopped_out_flat", realized=self.position.realized(),
                  ends_cycle=self.profile.ends_on_both_stopped)
        if self.profile.ends_on_both_stopped:
            self._end_cycle("both legs stopped out")

    def _enforce_protection(self):
        """No short may exist without a stop. Runs every poll as the backstop for
        any path that leaves a leg unprotected — arm it once more, and if that
        still fails, cover the leg."""
        for leg in list(self.position.unprotected_legs()):
            trigger = leg.sl_trigger or self.sl
            oid, ok, at_broker = self.executor.place_stop(leg, trigger)
            if ok:
                leg.mark_protected(oid, trigger, at_broker, self._hm())
                self._log("stop_placed", side=leg.opt_type, strike=leg.strike,
                          trigger=trigger, order=oid, recovered=True)
                continue
            self._log("unprotected_cover", side=leg.opt_type, strike=leg.strike,
                      reason="stop could not be armed")
            self._cover_and_retire(leg, "no stop — covered for safety")

    # ── single legs ──────────────────────────────────────────────────────
    def _build_leg(self, side: str, cand: dict):
        """Turn a selection into a placeable leg. The symbol and contract value
        come from the chain, which built them from Delta's own product master, so
        the contract we priced is by construction the contract we trade."""
        c = self.chain_obj.contract(cand["strike"], side)
        if not c:
            self._log("leg_build_failed", side=side, strike=cand["strike"],
                      reason="strike not in the chain")
            return None
        return Leg(side, cand["strike"], c["symbol"], self.contracts,
                   contract_value=c["contract_value"], product_id=c["product_id"],
                   otm_level=cand.get("otm_level"), reason=cand.get("why"))

    def _open_leg(self, leg) -> bool:
        """Sell one leg and protect it. True only when the leg ends up SHORT AND
        PROTECTED — any other outcome is unwound here, so the caller never has to
        reason about a half-open leg."""
        fill = self.executor.sell(leg)
        if not fill.ok:
            self._log("entry_nofill", side=leg.opt_type, strike=leg.strike,
                      status=fill.status, why=(fill.message or "")[:160])
            return False
        leg.mark_filled(fill.order_id, fill.price, fill.time,
                        crossed=fill.crossed, fee=fee_of(fill))
        oid, ok, at_broker = self.executor.place_stop(leg, self.sl)
        if not ok:
            self._log("stop_failed", side=leg.opt_type, strike=leg.strike,
                      trigger=self.sl)
            self._cover(leg, "stop rejected — leg unwound")
            return False
        leg.mark_protected(oid, self.sl, at_broker, self._hm())
        self._log("leg_open", side=leg.opt_type, strike=leg.strike,
                  entry=leg.entry_price, sl=self.sl, otm=leg.otm_level,
                  contracts=leg.contracts, crossed=fill.crossed,
                  fee=round(fill.fee, 4), why=leg.reason)
        return True

    def _cover(self, leg, reason: str) -> float | None:
        """Cancel a leg's stop, then buy it back. The cancel MUST come first — a
        stop left armed after the leg is closed would fire later and open a new,
        unwanted long.

        Returns the fill price, or None if the buy did NOT fill. A leg that does
        not fill is LEFT OPEN in the book. Writing it closed at an assumed price is
        how a failed cover becomes an invisible naked short: the book goes flat,
        MTM stops moving, the max-loss check can never fire again, and nothing
        tries a second time.
        """
        if not self.executor.cancel_stop(leg):
            self._log("stop_cancel_failed", side=leg.opt_type, strike=leg.strike)
        fill = self.executor.buy(leg, kind="exit")
        if not fill.ok:
            self._log("cover_failed", side=leg.opt_type, strike=leg.strike,
                      reason=reason, note="leg left OPEN — retrying every poll")
            return None
        leg.mark_closed(fill.price, reason, fill.order_id, fill.time,
                        fee=fill.fee, crossed=fill.crossed)
        self._log("leg_closed", side=leg.opt_type, strike=leg.strike,
                  exit=fill.price, reason=reason, pnl=leg.pnl(),
                  crossed=fill.crossed, fee=round(fill.fee, 4))
        return fill.price

    def _cover_and_retire(self, leg, reason: str) -> bool:
        side = leg.opt_type
        if self._cover(leg, reason) is None:
            self.stuck[side] = reason
            return False
        self.stuck.pop(side, None)
        self._retire(side)
        return True

    def _retry_stuck(self):
        """Retry every cover that has not filled, on every poll, until it does.

        This is the never-give-up path. A leg here is still short with no stop
        behind it, so it is the first thing the poll does and the last thing that
        would ever be allowed to be skipped.
        """
        for side in list(self.stuck):
            leg = self.position.leg(side)
            if leg is None or not leg.is_live:
                self.stuck.pop(side, None)
                continue
            reason = self.stuck[side]
            if self._cover(leg, reason) is not None:
                self.stuck.pop(side, None)
                self._retire(side)
                self._log("stuck_cleared", side=side, reason=reason)

    # ── windows ──────────────────────────────────────────────────────────
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

        The replacement is validated BEFORE anything is closed. If no strike
        qualifies the window is skipped whole and the position is left exactly as
        it is — closing the small leg first and only then discovering there is
        nothing to re-enter would strand the position single-legged for nothing.
        """
        pos = self.position
        open_side = PE if small_side == CE else CE
        open_leg = pos.leg(open_side)
        open_prem = self._mark(open_leg)
        small_leg = pos.leg(small_side)
        if open_prem is None:
            self._log("window_skipped", window=key, reason="no mark for the open leg")
            return

        cand = select_reentry_leg(self.chain_obj.chain(), self.chain_obj.atm,
                                  small_side, below_premium=open_prem,
                                  ratio=self.ratio, sl=self.sl)
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

        if not self._cover_and_retire(small_leg, f"adjustment {key} — 2x rule"):
            return          # old leg still open — do NOT stack a new one on top
        if self._open_leg(new_leg):
            self.position.set_leg(new_leg)
        else:
            self._log("adjust_incomplete", window=key, side=small_side,
                      note="replacement would not open — single-legged until next window")

    def _reenter_missing(self, key: str):
        """Re-open the side that was stopped out, by the same "just below" rule."""
        pos = self.position
        side = pos.missing_side()
        open_leg = pos.leg(PE if side == CE else CE)
        open_prem = self._mark(open_leg)
        if open_prem is None:
            self._log("window_skipped", window=key, reason="no mark for the open leg")
            return
        cand = select_reentry_leg(self.chain_obj.chain(), self.chain_obj.atm, side,
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
        if self._open_leg(leg):
            self.position.set_leg(leg)

    def _fresh_entry(self, reason: str):
        """Open a complete strangle at the target premium — both legs or neither."""
        if self.fresh_entries >= self.profile.max_fresh_entries:
            self._log("entry_blocked",
                      reason=f"max {self.profile.max_fresh_entries} fresh entries reached")
            return
        ch = self.chain_obj.chain()
        if self.chain_obj.atm is None or not ch:
            self._log("entry_blocked", reason="no chain/spot yet")
            return
        self.fresh_entries += 1
        self.expiry = self.chain_obj.expiry
        if self.first_entry_at is None:
            self.first_entry_at = self._now
            self.late_start = not self.profile.is_entry_time(self._now,
                                                             grace_seconds=300)

        picks = {}
        for side in (CE, PE):
            c = select_entry_leg(ch, self.chain_obj.atm, side, target=self.target,
                                 prefer_min=self.profile.prefer_min,
                                 prefer_max=self.profile.prefer_max)
            if c is None:
                self._log("entry_blocked", side=side, reason="no strike found in the chain")
                return
            picks[side] = c

        self._log("entry_start", reason=reason, spot=self.chain_obj.spot,
                  atm=self.chain_obj.atm, expiry=self.expiry, target=self.target,
                  sl=self.sl, contracts=self.contracts,
                  ce=f"{picks[CE]['strike']:.0f}@{picks[CE]['premium']}",
                  pe=f"{picks[PE]['strike']:.0f}@{picks[PE]['premium']}")

        legs = {}
        for side in (CE, PE):
            leg = self._build_leg(side, picks[side])
            if leg is None:
                self._unwind(legs, "leg could not be built")
                return
            if not self._open_leg(leg):
                # ATOMIC ENTRY: one leg failed -> immediately close whatever opened
                self._unwind(legs, f"{side} leg failed — no incomplete strangles")
                return
            legs[side] = leg

        for side, leg in legs.items():
            self.position.set_leg(leg)
        self._log("entry_complete",
                  ce=f"{legs[CE].strike:.0f}@{legs[CE].entry_price}",
                  pe=f"{legs[PE].strike:.0f}@{legs[PE].entry_price}",
                  combined=round((legs[CE].entry_price or 0)
                                 + (legs[PE].entry_price or 0), 2))

    def _unwind(self, legs: dict, reason: str):
        """Close every leg that opened during a failed entry. This is what makes
        the entry atomic."""
        for side, leg in legs.items():
            if leg.is_live:
                self.position.set_leg(leg)
                self._cover_and_retire(leg, f"entry unwound — {reason}")
        self._log("entry_aborted", reason=reason)

    def _flatten(self, reason: str):
        for side in (CE, PE):
            leg = self.position.leg(side)
            if leg is not None and leg.is_live:
                self._cover_and_retire(leg, reason)
        if self.stuck:
            self._log("flatten_failed", reason=reason, stuck=sorted(self.stuck),
                      note="STILL SHORT — retrying every poll until filled")
        else:
            self._log("flatten", reason=reason, realized=self.position.realized())

    # ── output ───────────────────────────────────────────────────────────
    def _record_equity(self, now: dt.datetime):
        """Sample MTM once a minute — enough to draw a month-long curve without
        writing a line every five seconds.

        A sample is SKIPPED while we hold legs we cannot price. This runs before
        the chain-ready check, so on a restart the first sample would otherwise be
        written with no marks — booking a live position at 0.00 and putting a
        false vertical drop on the equity curve at exactly the moment the engine
        came back. Better a one-minute hole than a fabricated number.
        """
        stamp = now.strftime("%Y-%m-%d %H:%M")
        if stamp == self._last_equity_min:
            return
        live = self.position.live_legs()
        if live and any(self._mark(l) is None for l in live):
            return
        self._last_equity_min = stamp
        if self.cycle is None and self.position.is_flat and not self.cycles_done:
            return
        row = {"ts": stamp, "profile": self.name, "cycle": self.cycle,
               "mtm": self.mtm, "realized": self.position.realized(),
               "closed_total": round(sum(c["realized"] for c in self.cycles_done), 4),
               "n_live": self.position.n_live,
               "spot": getattr(self.chain_obj, "spot", None)}
        self.equity.append(row)
        try:
            with open(self.results_dir / f"{self.name}_equity.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception:
            pass

    def snapshot(self) -> dict:
        marks = self.marks()
        closed = round(sum(c["realized"] for c in self.cycles_done), 4)
        return {
            "profile": self.name, "label": self.profile.label,
            "entry": str(self.profile.entry_time),
            "square_off": str(self.profile.square_off),
            "exposure_hours": self.profile.exposure_hours,
            "cycle": self.cycle, "expiry": self.expiry,
            "in_session": self.cycle is not None,
            "entered": self.entered, "done": self.done, "killed": self.killed,
            "kill_reason": self.kill_reason, "fresh_entries": self.fresh_entries,
            "windows_run": len(self.done_windows), "stuck": sorted(self.stuck) or None,
            "target": self.target, "sl": self.sl, "contracts": self.contracts,
            "max_loss": self.max_loss,
            "worst_case_both_stopped": self.profile.worst_case_both_stopped(),
            "spot": getattr(self.chain_obj, "spot", None),
            "atm": getattr(self.chain_obj, "atm", None),
            "position": self.position.to_dict(marks),
            "mtm": self.mtm, "closed_total": closed,
            "total": round(closed + self.mtm, 4),
            "cycles_completed": len(self.cycles_done),
            "next_window": (self.profile.next_window(self._now).strftime("%H:%M")
                            if self._now and self.profile.next_window(self._now) else None),
            "updated": self._stamp(),
        }

    # ── surviving a restart ──────────────────────────────────────────────
    def _resume_file(self) -> Path:
        return self.state_dir / f"{self.name}_RESUME.json"

    def _save_resume(self):
        """Everything needed to pick a cycle back up where it left off.

        B and C hold a position for 23.6 hours. Without this, any restart inside
        that window — a reboot, a stall exit, a deploy — silently abandons the
        book and the profile re-enters as a `late_start`, which is then excluded
        from the headline. A month with three restarts would quietly delete three
        of B's ~30 cycles while leaving A almost untouched, and the comparison
        would be measuring uptime rather than strategy.
        """
        try:
            self._resume_file().write_text(json.dumps({
                "cycle": self.cycle, "expiry": self.expiry,
                "entered": self.entered, "done": self.done, "killed": self.killed,
                "kill_reason": self.kill_reason, "fresh_entries": self.fresh_entries,
                "done_windows": sorted(self.done_windows),
                "stuck": self.stuck, "late_start": self.late_start,
                "first_entry_at": (self.first_entry_at.isoformat()
                                   if self.first_entry_at else None),
                "position": self.position.to_dict(self.marks()),
                "cycles_done": self.cycles_done,
            }, default=str), encoding="utf-8")
        except Exception:
            pass

    def restore(self, now: dt.datetime) -> bool:
        """Reload an interrupted cycle. True if anything was restored.

        Restores ONLY when the saved cycle is the cycle that is running right now.
        A stale file from a cycle that has since ended must never resurrect a
        position: that book was either squared off or is gone, and re-adopting it
        would invent a holding that does not exist.

        This is safe precisely because the book is PAPER. A live book must never
        trust a file for what it holds — it reconciles against the exchange, which
        is the only authority on a real position.
        """
        key = self.profile.cycle_key(now)
        if key is None:
            return False
        f = self._resume_file()
        if not f.exists():
            return False
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return False
        self.cycles_done = d.get("cycles_done") or []
        if d.get("cycle") != key:
            return False
        self._now = now
        self.cycle = key
        self.expiry = d.get("expiry")
        self.entered = bool(d.get("entered"))
        self.done = bool(d.get("done"))
        self.killed = bool(d.get("killed"))
        self.kill_reason = d.get("kill_reason")
        self.fresh_entries = int(d.get("fresh_entries") or 0)
        self.done_windows = set(d.get("done_windows") or [])
        self.stuck = dict(d.get("stuck") or {})
        self.late_start = bool(d.get("late_start"))
        fe = d.get("first_entry_at")
        self.first_entry_at = dt.datetime.fromisoformat(fe) if fe else None
        self.position = Position.from_dict(d.get("position") or {})
        self._log("resumed", legs=self.position.n_live,
                  realized=self.position.realized(),
                  windows_done=len(self.done_windows),
                  note="cycle picked back up after a restart")
        return True

    def _write_state(self):
        self._save_resume()
        try:
            (self.state_dir / f"{self.name}_STATE.json").write_text(
                json.dumps(self.snapshot(), indent=1, default=str), encoding="utf-8")
        except Exception:
            pass


def fee_of(fill) -> float:
    return getattr(fill, "fee", 0.0) or 0.0
