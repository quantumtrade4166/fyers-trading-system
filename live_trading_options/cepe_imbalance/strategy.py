"""
cepe_imbalance/strategy.py — the owner's CE/PE imbalance rules (agreed 2026-11-03).
===================================================================================

One Strategy instance per underlying. The engine calls, on the feed thread, under one lock:
  on_start(ctx)          Start button
  on_tick(ctx, keys)     every batch of ticks that touched this underlying
  on_timer(ctx)          once a second while running
  on_session_end(ctx)    at square-off time (exit everything)

RULES (owner, 3-Nov-2026). "Unit" = `lots` lots (default 1). Strikes on a fixed grid of `step`
(CRUDEOIL 200, GOLD/GOLDM 500, SILVER/SILVERM 1000, NIFTY 50, SENSEX 100).

ENTRY (Start button, in session): SELL CE at ATM, ATM+1, ATM+2 and PE at ATM, ATM-1, ATM-2.
  Pre-check: if those 6 already show |imbalance| > band, apply B to the PLAN first (no sell-then-buy).

HARD RULE — always short at ATM, both CE and PE, checked on EVERY tick:
  if no short CE (PE) at the current ATM -> buy back the CE (PE) farthest from ATM, sell the ATM one.
  ATM uses a buffer: it only moves once price is `atm_buffer_pct`% of a step past the half-way point,
  so a price sitting between two strikes cannot make it flip back and forth.
  ITM legs are NOT closed by this rule; they wait for an imbalance breach.

IMBALANCE = (short CE premium - short PE premium) / (sum) x 100, all current shorts,
  premium = LTP kept inside the live bid/ask (a stale last trade can't distort it).
  A single tick beyond +/-band (15%) fires an adjustment on that same tick. CE heavy (market up):
  A  repeat while possible: buy back the LOWEST CE, sell the strike just beyond the farthest CE.
     Never removes the last CE lot at ATM (hard rule).
  B  when A is not possible / did not bring it inside: buy back the farthest PE and sell it at the
     next PE strike we already hold (above it) -> more PE premium. Never goes past ATM.
  PE heavy (market down) is the exact mirror.
  Lots stay equal on both sides (every adjustment moves a unit within one side) -> no naked shorts.

LIQUIDITY GUARD (owner, 3-Nov): never SELL a strike whose bid-ask gap > max_spread_pct (3%) of its mid
  (or 2 ticks if bigger). The whole entry / adjustment waits for a tight quote; buy-backs never wait.

  C  (owner 3-Nov, expiry mornings) only when A and B have nothing left and the light side is all at
     ATM: move 1 unit of the light side from ATM to ITM1 (CE: ATM-1 strike, PE: ATM+1), then a 2nd
     unit if still beyond band; 1 unit always stays at ATM. Also used in the pre-entry check.

BACK TO NORMAL — "go normal whenever possible" (owner 3-Nov): every tick, after the ATM hard rule and
  BEFORE A/B/C, price the clean ATM/+-1/+-2 set around the current ATM; if its imbalance is inside
  +-normal_band (5%) the book switches to it (only differing strikes trade). So an ITM leg or a B/C
  doubling stays only while going clean would leave the imbalance outside +-5%.
RE-CENTRE (owner 3-Nov): if BOTH sides are adjusted (doubled strike or ITM leg on each), they cancel
  out -> rebuild like entry (clean set + B/C on the light side only) when that plan is inside +-band.

STOP: MTM <= -mtm_stop (rupees, editable, 0 = off) -> exit everything, no re-entry until Start.
"""
import time

from live_trading_options.cepe_imbalance import config as C


class Strategy:
    params: dict = {}

    def on_start(self, ctx):
        pass

    def on_tick(self, ctx, keys):
        pass

    def on_timer(self, ctx):
        pass

    def on_session_end(self, ctx):
        ctx.book.exit_all("session end")


class Idle(Strategy):
    """Does nothing (manual paper orders only)."""


class CePeImbalance(Strategy):
    params = {
        "lots": 1,               # lots per strike unit
        "band": 15.0,            # |imbalance| above this -> adjust (single tick)
        "normal_band": 5.0,      # back-to-normal threshold
        "atm_buffer_pct": 20.0,  # ATM moves only this % of a step past the half-way point
        "mtm_stop": 0.0,         # rupees; 0 = off
        "step": 0,               # strike grid; 0 = config default for the underlying
        "max_actions": 8,        # safety: adjustments allowed in one tick
        "max_spread_pct": 3.0,   # never SELL a strike whose bid-ask gap is wider than this % of its mid
    }                            # (or 2 ticks, whichever is bigger). Buy-backs / exits are never held.

    @staticmethod
    def spread_ok(ctx, c):
        """Owner 3-Nov: don't sell into a wide quote (GOLD/SILVER at the 09:05 open filled 40% under value)."""
        q = ctx.quote(c.key)
        if not q or not q.bid or not q.ask or q.bid <= 0 or q.ask < q.bid:
            return False, "no two-sided quote"
        gap, mid = q.ask - q.bid, (q.ask + q.bid) / 2
        limit = max(float(ctx.params.get("max_spread_pct", 3.0)) / 100 * mid, 2 * (c.tick or 0))
        return gap <= limit, f"gap {gap:g} = {gap / mid * 100:.1f}% (limit {limit:g})"

    # ── helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def step(ctx) -> float:
        return float(ctx.params.get("step") or C.STRIKE_STEP[ctx.name])

    @staticmethod
    def unit(ctx) -> int:
        return max(1, int(ctx.params.get("lots") or 1))

    def current_atm(self, ctx):
        """Grid ATM with hysteresis. Stored in ctx.state['atm']."""
        ref = ctx.ref_price()
        if not ref or ref <= 0:
            return ctx.state.get("atm")
        s = self.step(ctx)
        near = round(ref / s) * s
        cur = ctx.state.get("atm")
        if cur is None:
            new = near
        else:
            buf = float(ctx.params.get("atm_buffer_pct", 20.0)) / 100.0
            new = near if abs(ref - cur) > s * (0.5 + buf) else cur
        if new != cur:
            ctx.state["atm"] = new
            if cur is not None:
                ctx.log("atm_change", old=cur, new=new, ref=ref)
        return new

    @staticmethod
    def legs(ctx) -> dict:
        """Current net shorts of the chosen expiry: {'CE': {strike: lots}, 'PE': {...}}."""
        out = {"CE": {}, "PE": {}}
        for p in ctx.book.positions.values():
            if p.lots < 0 and p.kind in out and p.expiry == ctx.expiry:
                out[p.kind][p.strike] = out[p.kind].get(p.strike, 0) + (-p.lots)
        return out

    @staticmethod
    def price(ctx, c) -> float:
        q = ctx.quote(c.key) if c else None
        if not q:
            return 0.0
        return q.mark() or 0.0

    def plan_imbalance(self, ctx, legs):
        """Imbalance % of a hypothetical set of shorts. None if any price is missing."""
        tot = {"CE": 0.0, "PE": 0.0}
        for kind in ("CE", "PE"):
            for k, lots in legs[kind].items():
                c = ctx.contract(k, kind)
                px = self.price(ctx, c)
                if not c or px <= 0:
                    return None
                tot[kind] += px * lots * c.lot * c.mult
        t = tot["CE"] + tot["PE"]
        return (tot["CE"] - tot["PE"]) / t * 100 if t > 0 else None

    def blocked(self, ctx, why, key=None, **f):
        """Show a 'cannot act' reason; log it once per reason (`key`), not every tick."""
        ctx.state["blocked"] = why
        self._blk = True
        key = key or why
        now = getattr(self, "_keys_now", None)
        if now is None:                                    # called outside evaluate(): old one-key memory
            if ctx.state.get("blocked_key") != key:
                ctx.state["blocked_key"] = key
                ctx.log("blocked", why=why, **f)
            return
        # Several moves can wait at once (4-Nov fall-through): log each reason once while it stays waiting.
        if key not in now and key not in (ctx.state.get("blocked_keys") or ()):
            ctx.log("blocked", why=why, **f)
        now.add(key)

    # ── move builders (pure: take legs, return ops list or None) ───────────
    # op = (kind, strike, "BUY"|"SELL", lots)
    def op_A(self, legs, heavy, atm, step, unit):
        """A: roll the heavy side's nearest-the-money leg out beyond its farthest leg."""
        side = legs[heavy]
        if not side:
            return None
        if heavy == "CE":
            first, target = min(side), max(side) + step
            ok = first < atm or (first == atm and side[first] > unit)
        else:
            first, target = max(side), min(side) - step
            ok = first > atm or (first == atm and side[first] > unit)
        if not ok:
            return None
        return [(heavy, first, "BUY", unit), (heavy, target, "SELL", unit)]

    def op_B(self, legs, heavy, atm, unit):
        """B: on the light side, move the farthest leg in to the next strike we already hold."""
        light = "PE" if heavy == "CE" else "CE"
        side = legs[light]
        if len(side) < 2:
            return None
        ks = sorted(side)
        if light == "PE":
            far, inner = ks[0], ks[1]
            if inner > atm:
                return None
        else:
            far, inner = ks[-1], ks[-2]
            if inner < atm:
                return None
        return [(light, far, "BUY", unit), (light, inner, "SELL", unit)]

    def op_C(self, legs, heavy, atm, step, unit):
        """C (owner 3-Nov, expiry mornings): the light side is all at ATM and B has nothing left ->
        move one unit from ATM to ITM1 (CE: ATM-1 strike, PE: ATM+1). At most 2 units go to ITM1;
        one always stays at ATM (hard rule). Only from the pure ATM / ITM1 shape."""
        light = "PE" if heavy == "CE" else "CE"
        side = legs[light]
        itm1 = atm - step if light == "CE" else atm + step
        if not side or set(side) - {atm, itm1}:
            return None
        if side.get(atm, 0) <= unit or side.get(itm1, 0) >= 2 * unit:
            return None
        return [(light, atm, "BUY", unit), (light, itm1, "SELL", unit)]

    def op_rebuild(self, legs, kind, atm, step, unit):
        """Ops that turn one side into the clean ATM / +-1 / +-2 set (buy-backs first). None if same size differs."""
        side, want = legs[kind], self.clean(kind, atm, step, unit)
        if side == want or sum(side.values()) != sum(want.values()):
            return None
        keys = sorted(set(side) | set(want))
        buys = [(kind, k, "BUY", side.get(k, 0) - want.get(k, 0)) for k in keys if side.get(k, 0) > want.get(k, 0)]
        sells = [(kind, k, "SELL", want.get(k, 0) - side.get(k, 0)) for k in keys if want.get(k, 0) > side.get(k, 0)]
        return buys + sells

    def op_go_normal(self, legs, atm, step, unit):
        """Ops that turn the whole book into the clean set around `atm` (all buy-backs first). None if
        it is already clean or a side has a different number of lots (leave such a book alone)."""
        buys, sells = [], []
        for kind in ("CE", "PE"):
            if not legs[kind]:
                return None
            ops = self.op_rebuild(legs, kind, atm, step, unit)
            if ops is None and legs[kind] != self.clean(kind, atm, step, unit):
                return None                                      # size differs: not ours to reshape
            for o in ops or []:
                (buys if o[2] == "BUY" else sells).append(o)
        return (buys + sells) or None

    def op_atm(self, legs, kind, atm, unit):
        """Hard rule: no short at ATM on this side -> move the leg farthest from ATM to ATM."""
        side = legs[kind]
        if side.get(atm):
            return None
        if not side:
            return [(kind, atm, "SELL", unit)]
        otm = (lambda k: k) if kind == "CE" else (lambda k: -k)          # tie -> the more OTM leg
        far = max(side, key=lambda k: (abs(k - atm), otm(k)))
        return [(kind, far, "BUY", unit), (kind, atm, "SELL", unit)]

    def clean(self, kind, atm, step, unit):
        sgn = 1 if kind == "CE" else -1
        return {atm + sgn * i * step: unit for i in range(3)}

    def op_normal(self, legs, atm, step, unit):
        """Re-split sides holding doubled strikes back to the clean 3-strike set."""
        ops = []
        for kind in ("CE", "PE"):
            side = legs[kind]
            if not any(v > unit for v in side.values()):
                continue
            want = self.clean(kind, atm, step, unit)
            if sum(side.values()) != sum(want.values()):
                continue                                         # different size: leave it
            for k in sorted(set(side) | set(want)):
                d = want.get(k, 0) - side.get(k, 0)
                if d < 0:
                    ops.append((kind, k, "BUY", -d))
            for k in sorted(set(side) | set(want)):
                d = want.get(k, 0) - side.get(k, 0)
                if d > 0:
                    ops.append((kind, k, "SELL", d))
        return ops or None

    @staticmethod
    def apply_ops(legs, ops):
        new = {"CE": dict(legs["CE"]), "PE": dict(legs["PE"])}
        for kind, k, side, lots in ops:
            v = new[kind].get(k, 0) + (lots if side == "SELL" else -lots)
            if v:
                new[kind][k] = v
            else:
                new[kind].pop(k, None)
        return new

    # ── execution ───────────────────────────────────────────────────────────
    def execute(self, ctx, ops, reason) -> bool:
        cs = []
        for kind, k, side, lots in ops:
            c = ctx.contract(k, kind)
            if not c or not ctx.book.fill_price(c.key, side):
                self.blocked(ctx, f"{reason}: no price for {kind} {k:g}", key=f"no price {kind} {k:g}")
                self._fail = "price"
                return False
            if side == "SELL":
                ok, why = self.spread_ok(ctx, c)
                if not ok:
                    self.blocked(ctx, f"{reason}: waiting, {k:g} {kind} quote too wide ({why})",
                                 key=f"wide {reason.split(' (')[0]} {k:g} {kind}")
                    self._fail = "wide"
                    return False
            cs.append((c, side, lots))
        before = ctx.book.imbalance()["pct"]
        fills = []
        for c, side, lots in cs:                                    # buy-backs come first in every op list
            t = ctx.book.order(c, side, lots, reason)
            fills.append(f"{side} {lots} {c.symbol} @{t.get('price')}" if t.get("ok") else f"FAILED {c.symbol}")
            if not t.get("ok"):
                ctx.log("order_failed", reason=reason, error=t.get("error"))
                self._fail = "order"
                return False
        ctx.state["blocked"] = ctx.state["blocked_key"] = None
        ctx.log("adjust", action=reason, imb_before=before, imb_after=ctx.book.imbalance()["pct"],
                atm=ctx.state.get("atm"), fills=fills)
        return True

    # ── rules ───────────────────────────────────────────────────────────────
    def plan_book(self, ctx, atm):
        """Clean set at `atm`, then B (then C) on the light side only until |imbalance| <= band.
        Returns (legs, notes) or (None, reason). Used for entry and for re-centre."""
        step, unit, band = self.step(ctx), self.unit(ctx), float(ctx.params["band"])
        legs = {"CE": self.clean("CE", atm, step, unit), "PE": self.clean("PE", atm, step, unit)}
        imb = self.plan_imbalance(ctx, legs)
        if imb is None:
            return None, "waiting for prices of the 6 strikes"
        notes, light = [], None
        while abs(imb) > band:
            heavy = "CE" if imb > 0 else "PE"
            if light and light == heavy:                           # overshot: never adjust both sides
                break
            ops, tag = self.op_B(legs, heavy, atm, unit), "B"
            if not ops:
                ops, tag = self.op_C(legs, heavy, atm, step, unit), "C"
            if not ops:
                break
            light = ops[0][0]
            legs = self.apply_ops(legs, ops)
            notes.append(f"{tag} {ops[0][0]} {ops[0][1]:g}->{ops[1][1]:g}")
            nxt = self.plan_imbalance(ctx, legs)
            if nxt is None:
                return None, "waiting for prices of the ITM1 strike"
            imb = nxt
        return legs, notes

    @staticmethod
    def op_to(legs, target):
        """Ops turning `legs` into `target` (buy-backs first)."""
        buys, sells = [], []
        for kind in ("CE", "PE"):
            cur, want = legs[kind], target[kind]
            for k in sorted(set(cur) | set(want)):
                d = want.get(k, 0) - cur.get(k, 0)
                if d < 0:
                    buys.append((kind, k, "BUY", -d))
                elif d > 0:
                    sells.append((kind, k, "SELL", d))
        return buys + sells

    def entry(self, ctx, atm):
        step, unit, band = self.step(ctx), self.unit(ctx), float(ctx.params["band"])
        rq = ctx.quote(ctx.market.ref_key(ctx.name, ctx.expiry))
        if rq and rq.recv_ts and time.time() - rq.recv_ts > 60:          # no entry on a stale price (holiday)
            return self.blocked(ctx, "entry: no live price in the last 60 s (market closed?)")
        legs, notes = self.plan_book(ctx, atm)
        if legs is None:
            return self.blocked(ctx, f"entry: {notes}", key="entry prices")
        ops = [(k, s, "SELL", n) for k in ("CE", "PE") for s, n in sorted(legs[k].items())]
        if self.execute(ctx, ops, "entry" + (" (pre-" + ", pre-".join(notes) + ")" if notes else "")):
            ctx.state["phase"] = "in"
            ctx.state["entry_atm"] = atm

    def next_action(self, ctx, atm):
        """First candidate move (or None). Kept for tests / callers wanting just the top move."""
        return next(self.candidates(ctx, atm), None)

    def candidates(self, ctx, atm):
        """Moves in priority order. evaluate() tries each in turn: if one is held by a wide quote, the
        next is tried (owner 4-Nov: while the ATM leg waits for a fair quote, keep balancing the book
        with strikes that do have fair quotes, e.g. roll ITM CEs out with A)."""
        step, unit, band = self.step(ctx), self.unit(ctx), float(ctx.params["band"])
        legs = self.legs(ctx)
        for kind in ("CE", "PE"):                                  # hard rule first
            ops = self.op_atm(legs, kind, atm, unit)
            if ops:
                yield ops, f"ATM rule {kind}"
        im = ctx.book.imbalance()
        imb = im["pct"]
        if imb is None:
            if im.get("missing"):
                self.blocked(ctx, "waiting for prices of " + ", ".join(im["missing"]), key="missing prices")
            return
        # GO NORMAL WHENEVER POSSIBLE (owner 3-Nov): if the clean ATM/+-1/+-2 set, priced now, is inside
        # +-normal_band, switch to it (only the differing strikes trade). Checked every tick, before A/B/C.
        ops = self.op_go_normal(legs, atm, step, unit)
        if ops:
            after = self.plan_imbalance(ctx, self.apply_ops(legs, ops))
            if after is not None and abs(after) <= float(ctx.params["normal_band"]):
                yield ops, f"back to normal (clean set {after:+.1f}%)"
        # RE-CENTRE (owner 3-Nov): both sides adjusted (doubled strike / ITM leg) = they prop each other up.
        # Rebuild like entry: clean set + B/C on the light side only, if that plan is inside +-band.
        clean = {k: self.clean(k, atm, step, unit) for k in ("CE", "PE")}
        if legs["CE"] != clean["CE"] and legs["PE"] != clean["PE"]                 and sum(legs["CE"].values()) == sum(legs["PE"].values()) == 3 * unit:
            target, notes = self.plan_book(ctx, atm)
            if target is not None and target != legs and (target["CE"] == clean["CE"] or target["PE"] == clean["PE"]):
                after = self.plan_imbalance(ctx, target)
                if after is not None and abs(after) <= band:
                    yield self.op_to(legs, target), f"re-centre ({after:+.1f}%{', ' + ', '.join(notes) if notes else ''})"
        if abs(imb) <= band:
            return
        heavy = "CE" if imb > 0 else "PE"
        found = False
        for tag, ops in (("A", self.op_A(legs, heavy, atm, step, unit)),
                         ("B", self.op_B(legs, heavy, atm, unit)),
                         ("C", self.op_C(legs, heavy, atm, step, unit))):
            if ops:
                found = True
                yield ops, f"{tag} ({heavy} heavy {imb:+.1f}%)"
        if not found:
            self.blocked(ctx, f"{heavy} heavy {imb:+.1f}%: no A, B or C left", key=f"{heavy} heavy: no A/B/C")

    def evaluate(self, ctx):
        self._keys_now = set()
        try:
            self._evaluate(ctx)
        finally:
            ctx.state["blocked_keys"] = sorted(self._keys_now)
            self._keys_now = None

    def _evaluate(self, ctx):
        ph = ctx.state.get("phase")
        if ph not in ("wait_entry", "in") or not ctx.in_session():
            return
        stop = float(ctx.params.get("mtm_stop") or 0)
        if ph == "in" and stop > 0 and ctx.book.mtm()["total"] <= -stop:
            out = ctx.book.exit_all("MTM stop loss")
            ctx.state["phase"] = "stopped"
            ctx.log("stop_loss", mtm=ctx.book.mtm()["total"], exits=len(out))
            ctx.save()
            return
        atm = self.current_atm(ctx)
        if atm is None:
            return
        if ph == "wait_entry":
            self.entry(ctx, atm)
            ctx.save()
            return
        acted = False
        self._blk = False
        for _ in range(int(ctx.params.get("max_actions", 8))):
            done = False
            for ops, reason in self.candidates(ctx, atm):          # held by a wide quote -> try the next
                self._fail = None
                if self.execute(ctx, ops, reason):
                    done = True
                    break
                if self._fail == "order":
                    break
            if not done:
                break
            acted = True
        else:
            self.blocked(ctx, "max actions in one tick reached")
        if not self._blk and ctx.state.get("blocked"):
            ctx.state["blocked"] = ctx.state["blocked_key"] = None          # nothing is waiting any more
        if acted:
            ctx.save()

    # ── hooks ───────────────────────────────────────────────────────────────
    def on_start(self, ctx):
        has = any(p.lots for p in ctx.book.positions.values())
        ctx.state["phase"] = "in" if has else "wait_entry"
        ctx.state["blocked"] = None
        ctx.log("phase", phase=ctx.state["phase"])
        self.evaluate(ctx)

    def on_tick(self, ctx, keys):
        self.evaluate(ctx)

    def on_timer(self, ctx):
        self.evaluate(ctx)

    def on_session_end(self, ctx):
        ctx.book.exit_all("session end")
        ctx.state["phase"] = "done"


REGISTRY = {"cepe": CePeImbalance, "idle": Idle}
DEFAULT = "cepe"
