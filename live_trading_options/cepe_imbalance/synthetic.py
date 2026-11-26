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
        "entry_gap": 0.0,        # entry waits while |synthetic - index carried to expiry| > this; 0 = 40% of a strike
    }
    MAX_ADJ_PER_MIN = 20
    FAST_ORDERS = True           # 26-Nov: live orders re-priced every 0.7 s (not 2-3 s) - see live.WAIT_FAST_S
    CARRY_PCT_YEAR = 6.5         # index -> forward for the entry check (synthetic sits ~ this much above spot)         # loop safety (to-and-fro returns to old legs on purpose: no repeat-book guard)

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
    def entry_ready(self, ctx, atm):
        """26-Nov-2026 (owner): at 09:15:10 SENSEX's synthetic was 83,932 while the index was 84,030 - option
        LTPs had not traded since the open yet - so the book sold 'ATM' 83900 with the real ATM at 84100 and
        lost ~Rs 10.5k in the first second. Entry now waits until the synthetic agrees with the index (carried
        to expiry) within `entry_gap` (default 40% of a strike). -> (ok, why)"""
        syn = ctx.state.get("syn")
        ref = ctx.market.ref_price(ctx.name, ctx.expiry)
        if syn is None or not ref:
            return False, "no synthetic / index price yet"
        try:
            days = max(0, (dt.date.fromisoformat(str(ctx.expiry)) - ctx.now.astimezone(IST).date()).days)
        except ValueError:
            days = 0
        fwd = ref * (1 + self.CARRY_PCT_YEAR / 100 * days / 365)
        tol = float(ctx.params.get("entry_gap") or 0) or 0.4 * self.step(ctx)
        gap = syn - fwd
        if abs(gap) > tol:
            return False, (f"entry: waiting - synthetic {syn:,.1f} vs index {ref:,.1f} (expected {fwd:,.1f}, "
                           f"gap {gap:+.0f} > {tol:.0f}): option prices not settled yet")
        return True, ""

    def entry(self, ctx, atm):
        rq = ctx.quote(ctx.market.ref_key(ctx.name, ctx.expiry))
        if rq and rq.recv_ts and time.time() - rq.recv_ts > 60:
            return self.blocked(ctx, "entry: no live price in the last 60 s (market closed?)")
        ok, why = self.entry_ready(ctx, atm)
        if not ok:
            return self.blocked(ctx, why, key="entry gap")
        step, unit = self.step(ctx), max(1, int(ctx.params.get("lots") or 1))
        legs = {"CE": self.clean("CE", atm, step, unit), "PE": self.clean("PE", atm, step, unit)}
        ops = self.paired([(k, st, "SELL", n) for k in ("CE", "PE") for st, n in sorted(legs[k].items())], atm)
        ctx.state["entry_syn"] = ctx.state.get("syn")
        self.run_entry(ctx, ops, atm, f"entry (synthetic {ctx.state.get('syn'):g} -> ATM {atm:g})")

    def run_entry(self, ctx, ops, atm, label):
        """Entry legs go out one CE / PE pair at a time. 26-Nov (owner): before every later pair the ATM is read
        again - if the market has moved a strike while the first pairs were filling, the remaining legs are
        placed around the NEW ATM (same distance from it), never at strikes the market has already left.
        Legs that could not be placed are kept in state 'entry_left' and retried on the next tick."""
        step = self.step(ctx)
        pairs = [list(ops[i:i + 2]) for i in range(0, len(ops), 2)]
        for i in range(len(pairs)):
            if i or ctx.state.get("phase") == "in":
                new = self.current_atm(ctx)
                if new is not None and new != atm:
                    held = self.legs(ctx)
                    taken = {k: set(held[k]) for k in ("CE", "PE")}
                    moved = []
                    for pr in pairs[i:]:
                        q = []
                        for kind, k, side, n in pr:
                            k2 = k + (new - atm)
                            out = step if kind == "CE" else -step
                            while k2 in taken[kind]:          # never double a strike already sold
                                k2 += out
                            taken[kind].add(k2)
                            q.append((kind, k2, side, n))
                        moved.append(q)
                    pairs[i:] = moved
                    ctx.log("entry_recentre", frm=atm, to=new, legs=[f"{o[0]} {o[1]:g}" for pr in moved for o in pr])
                    atm = new
            tag = f"{label} [{i + 1}/{len(pairs)}]" if len(pairs) > 1 else label
            if not self.execute(ctx, pairs[i], tag):
                left = [list(o) for pr in pairs[i:] for o in pr]
                ctx.state["entry_left"] = left if ctx.state.get("phase") == "in" else None
                ctx.state["entry_left_atm"] = atm
                return False
            if ctx.state.get("phase") != "in":
                ctx.state["phase"] = "in"
                ctx.state["entry_atm"] = atm
        ctx.state["entry_left"] = None
        return True

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
        if ph == "in" and ctx.state.get("entry_left") and ctx.in_session():   # finish an interrupted entry first
            atm = self.current_atm(ctx)
            if atm is not None:
                left = [tuple(o) for o in ctx.state["entry_left"]]
                self.run_entry(ctx, left, ctx.state.get("entry_left_atm") or atm, "entry (rest)")
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


class SynToFroV2(SynToFro):
    """V2 (owner 25-Nov-2026): the SAME to-and-fro book and moves as V1 - only WHEN a move fires differs.
    V1 waits for the synthetic to cross a fixed level (strike +- half / one strike + buffer); V2 reads the
    option prices themselves (LTP, every tick, no buffer). K = 1st (nearest-money) sold CE, J = 1st sold PE.

      CALL OUT  LTP(CE K+1) >= LTP(PE K)      (owner: "2nd call = 1st put")    1st CE -> past the last CE
      PUT OUT   LTP(PE J-1) >= LTP(CE J)      (mirror)                          1st PE -> past the last PE
      CALL IN   LTP(CE K-1) <= LTP(PE K-1)    (owner: "ITM call = the ATM put") farthest CE -> K-1 (new ATM)
      PUT IN    LTP(PE J+1) <= LTP(CE J+1)    (mirror)                          farthest PE -> J+1 (new ATM)

    The put compared with the calls sits at the 1st CALL's strike (and the call at the 1st PUT's strike): the
    owner's examples are a balanced book where both are the ATM strike. Comparing with the literally held 1st
    put instead made V2 push calls out twice in a row right after a call out (that put is then one strike behind).
    So on smooth prices: call out at K + half a strike, put in at J + one strike - V1 with no buffer, read from
    the option prices. Outs before ins. Backtest / replay only until the owner approves it for live."""
    params = {**SynToFro.params, "buffer": 0.0}

    def _px(self, ctx, kind, k):
        return self._ltp(ctx, ctx.contract(k, kind))

    def triggers(self, ctx, legs=None):
        """[(move, (label, ltp), op, (label, ltp), due)] for the held book - for the screen and the rules."""
        legs = legs or self.legs(ctx)
        ce, pe = legs["CE"], legs["PE"]
        if not ce or not pe:
            return []
        s = self.step(ctx)
        K, J = min(ce), max(pe)
        rows = []

        def row(move, ak, aK, op, bk, bK):
            a, b = self._px(ctx, ak, aK), self._px(ctx, bk, bK)
            due = a is not None and b is not None and (a >= b if op == ">=" else a <= b)
            rows.append((move, (f"{aK:g} {ak}", a), op, (f"{bK:g} {bk}", b), due))
        row("call out", "CE", K + s, ">=", "PE", K)
        row("put out", "PE", J - s, ">=", "CE", J)
        row("call in", "CE", K - s, "<=", "PE", K - s)
        row("put in", "PE", J + s, "<=", "CE", J + s)
        return rows

    def levels(self, ctx, legs=None):
        return {"call_out": None, "call_in": None, "put_in": None, "put_out": None}

    def candidates(self, ctx, atm):
        legs, s = self.legs(ctx), self.step(ctx)
        ce, pe = legs["CE"], legs["PE"]
        if not ce or not pe:
            self.blocked(ctx, "one side has no short - the to-and-fro needs both CE and PE", key="side empty")
            return
        trig = {t[0]: t for t in self.triggers(ctx, legs)}
        missing = [f"{t[1][0]} / {t[3][0]}" for t in trig.values() if t[1][1] is None or t[3][1] is None]
        if missing:
            self.blocked(ctx, "V2 waiting for LTP of " + ", ".join(missing), key="v2 ltp")
        kc, kp = min(ce), max(pe)
        desc = lambda t: f"{t[1][0]} {t[1][1]:g} {t[2]} {t[3][0]} {t[3][1]:g}"
        outs = []
        if trig["call out"][4]:
            outs.append((self._roll("CE", kc, max(ce) + s, ce[kc]),
                         f"call out ({kc:g} CE -> {max(ce) + s:g}, {desc(trig['call out'])})"))
        if trig["put out"][4]:
            outs.append((self._roll("PE", kp, min(pe) - s, pe[kp]),
                         f"put out ({kp:g} PE -> {min(pe) - s:g}, {desc(trig['put out'])})"))
        if outs:
            yield from outs
            return
        if trig["put in"][4]:
            yield (self._roll("PE", min(pe), kp + s, pe[min(pe)]),
                   f"put in ({min(pe):g} PE -> {kp + s:g}, {desc(trig['put in'])})")
        if trig["call in"][4]:
            yield (self._roll("CE", max(ce), kc - s, ce[max(ce)]),
                   f"call in ({max(ce):g} CE -> {kc - s:g}, {desc(trig['call in'])})")

    def view(self, ctx) -> dict:
        v = super().view(ctx)
        v["version"] = 2
        v["triggers"] = [{"move": t[0], "a": t[1][0], "a_ltp": t[1][1], "op": t[2], "b": t[3][0], "b_ltp": t[3][1],
                          "due": t[4]} for t in self.triggers(ctx)]
        return v
