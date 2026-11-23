"""
cepe_imbalance/synthetic.py — SYNTHETIC TO-AND-FRO (owner's rules, agreed 23-Nov-2026).
========================================================================================

Books "NIFTY_SYN" / "SENSEX_SYN" (paper) and "NIFTY_SYN_LIVE" / "SENSEX_SYN_LIVE" (real Kotak P orders).
Same engine, feed, paper fills, live order book and MTM history as the CE/PE imbalance books; only the
rules differ. The CE/PE imbalance of the held book is shown on the screen, it never triggers anything.

SYNTHETIC = K + CE(LTP) - PE(LTP), K = the listed strike nearest the index (re-taken at the strike nearest
  the synthetic once, if that differs). Every tick.
GRID step = 50 NIFTY / 100 SENSEX.  Buffer = 5 points (editable).

ENTRY (entry time editable, or Start): ATM = synthetic rounded to the grid.
  SELL CE at ATM, ATM+1, ATM+2 and PE at ATM, ATM-1, ATM-2, `lots` lots each (CE/PE pairs, nearest first).

OUT = buy back a sold strike that went in the money and sell it one step PAST the last sold strike on that side.
IN  = buy back the farthest sold strike and sell it at ATM (one step inside the nearest sold strike).

  market up                                              market down (mirror)
  CALL OUT  synthetic >= lowest CE + step/2 + buffer     PUT OUT  synthetic <= highest PE - step/2 - buffer
            (NIFTY: 25000 CE out at 25030)                         (NIFTY: 25000 PE out at 24970)
  PUT IN    synthetic >= highest PE + step + buffer      CALL IN  synthetic <= lowest CE - step - buffer
            (NIFTY: 25000 PE in at 25055)                          (NIFTY: 25000 CE in at 24945)

  Owner's worked example: start 25000 -> CE 25000/25050/25100, PE 25000/24950/24900.
    25030 call out (25000 CE -> 25150), 25055 put in (24900 PE -> 25050), 25080 call out, 25105 put in ...
    back down: 25020 put out (25050 PE -> 24900), 24995 call in (25150 CE -> 25000) = the starting book.
  A big jump repeats the steps (one strike each) until no trigger is left; outs go before ins.

EXITS (all editable, 0 / blank = off): exit time (default 15:15 = the engine square-off), MTM stop loss,
  profit target, trailing profit lock, ATM CE+PE level exits — each exits everything, stopped for the day.
"""
import datetime as dt
import time

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance.strategy import CePeImbalance

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class SynToFro(CePeImbalance):
    params = {
        "lots": 1,               # lots per strike (3 CE + 3 PE strikes)
        "buffer": 5.0,           # points past the trigger (owner: midpoint + 5 out, strike + 5 in)
        "step": 0,               # strike grid; 0 = config default (NIFTY 50, SENSEX 100)
        "exit_time": "15:15",    # IST; exit all at / after this (the engine squares off at 15:15 anyway)
        "mtm_stop": 0.0,         # rupees; 0 = off
        "profit_target": 0.0,    # rupees; 0 = off
        "lock_start": 0.0,       # trailing profit lock: once the day's best MTM reaches this ...
        "lock_gap": 0.0,         # ... exit when MTM falls this far below the best (0 = off)
        "exit_above": 0.0,       # ATM CE + ATM PE >= this -> exit (0 = off)
        "exit_below": 0.0,       # ATM CE + ATM PE <= this -> exit (0 = off)
        "max_actions": 12,       # safety: adjustments in one tick (a 150-pt jump = 6)
        "max_spread_pct": 3.0,   # never SELL into a bid-ask gap wider than this % of mid
        "band": 0.0,             # unused here (kept: shared CE/PE code reads it)
    }
    MAX_ADJ_PER_MIN = 20         # loop safety (to-and-fro returns to old legs on purpose: no repeat-book guard)

    # ── synthetic price ─────────────────────────────────────────────────────
    @staticmethod
    def _ltp(ctx, c):
        q = ctx.quote(c.key) if c else None
        return float(q.ltp) if q and q.ltp and q.ltp > 0 else None

    def synthetic(self, ctx):
        """(synthetic, strike used) from ATM CE / PE LTPs, or (None, why)."""
        k = ctx.atm()
        if k is None:
            return None, "no index price"
        for _ in range(2):
            ce, pe = self._ltp(ctx, ctx.contract(k, "CE")), self._ltp(ctx, ctx.contract(k, "PE"))
            if ce is None or pe is None:
                return None, f"no LTP for {k:g} CE / PE"
            syn = k + ce - pe
            strikes = ctx.market.u.underlyings[ctx.name].strikes(ctx.expiry)
            k2 = min(strikes, key=lambda s: (abs(s - syn), s)) if strikes else k
            if k2 == k:
                break
            k = k2
        return round(syn, 2), k

    def current_atm(self, ctx):
        syn, k = self.synthetic(ctx)
        ctx.state["syn"], ctx.state["syn_k"] = syn, (k if syn is not None else None)
        if syn is None:
            self.blocked(ctx, f"waiting for the synthetic ({k})", key="synthetic")
            return None
        s = self.step(ctx)
        atm = round(syn / s) * s
        ctx.state["atm"] = atm
        return atm

    # ── triggers ────────────────────────────────────────────────────────────
    def levels(self, ctx, legs=None):
        """The four trigger prices for the held book (None where a side is empty)."""
        legs = legs or self.legs(ctx)
        s, b = self.step(ctx), float(ctx.params.get("buffer") or 0)
        ce, pe = legs["CE"], legs["PE"]
        kc, kp = (min(ce) if ce else None), (max(pe) if pe else None)
        return {"call_out": kc + s / 2 + b if kc is not None else None,
                "call_in": kc - s - b if kc is not None else None,
                "put_in": kp + s + b if kp is not None else None,
                "put_out": kp - s / 2 - b if kp is not None else None}

    @staticmethod
    def _roll(kind, frm, to, n):
        return [(kind, frm, "BUY", n), (kind, to, "SELL", n)]

    def candidates(self, ctx, atm):
        syn = ctx.state.get("syn")
        if syn is None:
            return
        legs, s = self.legs(ctx), self.step(ctx)
        ce, pe = legs["CE"], legs["PE"]
        if not ce or not pe:
            self.blocked(ctx, "one side has no short - the to-and-fro needs both CE and PE", key="side empty")
            return
        lv = self.levels(ctx, legs)
        kc, kp = min(ce), max(pe)
        if syn >= lv["call_out"]:                       # nearest CE in the money: push it past the last CE
            yield self._roll("CE", kc, max(ce) + s, ce[kc]), \
                f"call out ({kc:g} CE -> {max(ce) + s:g}, synthetic {syn:g} >= {lv['call_out']:g})"
        if syn <= lv["put_out"]:                        # nearest PE in the money: push it below the last PE
            yield self._roll("PE", kp, min(pe) - s, pe[kp]), \
                f"put out ({kp:g} PE -> {min(pe) - s:g}, synthetic {syn:g} <= {lv['put_out']:g})"
        if syn >= lv["put_in"]:                         # puts far out of the money: farthest PE in to ATM
            yield self._roll("PE", min(pe), kp + s, pe[min(pe)]), \
                f"put in ({min(pe):g} PE -> {kp + s:g}, synthetic {syn:g} >= {lv['put_in']:g})"
        if syn <= lv["call_in"]:                        # calls far out of the money: farthest CE in to ATM
            yield self._roll("CE", max(ce), kc - s, ce[max(ce)]), \
                f"call in ({max(ce):g} CE -> {kc - s:g}, synthetic {syn:g} <= {lv['call_in']:g})"

    def loop_ok(self, ctx, ops, reason) -> bool:
        m = int(time.time() // 60)
        cnt = ctx.state.get("adj_minute") or [m, 0]
        if cnt[0] == m and cnt[1] >= self.MAX_ADJ_PER_MIN:
            self.blocked(ctx, f"{reason}: held - {self.MAX_ADJ_PER_MIN} adjustments already this minute (safety cap)",
                         key="per-minute cap")
            return False
        return True

    # ── entry / exits ───────────────────────────────────────────────────────
    def entry(self, ctx, atm):
        rq = ctx.quote(ctx.market.ref_key(ctx.name, ctx.expiry))
        if rq and rq.recv_ts and time.time() - rq.recv_ts > 60:
            return self.blocked(ctx, "entry: no live price in the last 60 s (market closed?)")
        step, unit = self.step(ctx), max(1, int(ctx.params.get("lots") or 1))
        legs = {"CE": self.clean("CE", atm, step, unit), "PE": self.clean("PE", atm, step, unit)}
        ops = self.paired([(k, st, "SELL", n) for k in ("CE", "PE") for st, n in sorted(legs[k].items())], atm)
        if self.execute(ctx, ops, f"entry (synthetic {ctx.state.get('syn'):g} -> ATM {atm:g})"):
            ctx.state["phase"] = "in"
            ctx.state["entry_atm"] = atm
            ctx.state["entry_syn"] = ctx.state.get("syn")

    def _exit_time(self, ctx):
        t = str(ctx.params.get("exit_time") or "").strip()
        if not t:
            return None
        try:
            return dt.time.fromisoformat(t)
        except ValueError:
            return None

    def _evaluate(self, ctx):
        ph = ctx.state.get("phase")
        et = self._exit_time(ctx)
        if et and ph in ("wait_entry", "in") and ctx.now.astimezone(IST).time() >= et:
            out = ctx.book.exit_all(f"exit time {et.strftime('%H:%M:%S')}") if ph == "in" else []
            ctx.state["phase"] = "done"
            ctx.state["blocked"] = f"exit time {et.strftime('%H:%M:%S')} reached - done for the day"
            ctx.log("exit_time", time=et.strftime("%H:%M:%S"), mtm=ctx.book.mtm()["total"], exits=len(out))
            ctx.save()
            return
        super()._evaluate(ctx)

    # ── read model for the screen ───────────────────────────────────────────
    def view(self, ctx) -> dict:
        legs = self.legs(ctx)
        return {"syn": ctx.state.get("syn"), "syn_k": ctx.state.get("syn_k"), "atm": ctx.state.get("atm"),
                "entry_atm": ctx.state.get("entry_atm"), "entry_syn": ctx.state.get("entry_syn"),
                "levels": self.levels(ctx, legs),
                "legs": {k: sorted(([s, n] for s, n in v.items()), key=lambda x: x[0]) for k, v in legs.items()},
                "step": self.step(ctx), "buffer": float(ctx.params.get("buffer") or 0)}
