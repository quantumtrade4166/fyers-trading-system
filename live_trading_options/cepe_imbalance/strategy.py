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

BAND-ONLY (owner 9-Nov, replaces the 3-Nov "always short at ATM on every tick" rule): while
  |imbalance| <= band NOTHING trades, even if ATM moved, a leg went ITM or a strike is doubled.
  On a breach, first move that can go through (one wide quote -> try the next), repeated until inside:
    1. back to normal (clean set inside +-normal_band)   2. re-centre (both sides adjusted)
    3. ITM back: the heavy side's ITM leg rolled out (= A on it)
    4. roll light side to ATM: buy its farthest leg, sell the ATM strike (before B doubles a held one)
    5. A, B, C as below.
  EARLY NORMAL (owner 9-Nov): between `normal_trigger` (10%) and the band only step 1 may trade, so an
  ITM / doubled / off-centre book is cleaned while it is cheap. normal_trigger = band turns it off.
  ATM uses a buffer: it only moves once price is `atm_buffer_pct`% of a step past the half-way point,
  so a price sitting between two strikes cannot make it flip back and forth.

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

BACK TO NORMAL (owner 3-Nov, band-only since 9-Nov): on a breach, price the clean ATM/+-1/+-2 set
  around the current ATM; if its imbalance is inside +-normal_band (5%) switch to it (only differing
  strikes trade).
RE-CENTRE (owner 3-Nov, band-only since 9-Nov): if BOTH sides are adjusted (doubled strike or ITM leg
  on each) -> rebuild like entry (clean set + B/C on the light side only) when that plan is inside +-band.

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
        "normal_trigger": 10.0,  # owner 9-Nov: inside the band, beyond this -> "back to normal" only (= band: off)
        "atm_buffer_pct": 20.0,  # ATM moves only this % of a step past the half-way point
        "mtm_stop": 0.0,         # rupees; 0 = off
        "step": 0,               # strike grid; 0 = config default for the underlying
        "max_actions": 8,        # safety: adjustments allowed in one tick
        "max_spread_pct": 3.0,   # never SELL a strike whose bid-ask gap is wider than this % of its mid
        "exit_above": 0.0,       # owner 17-Nov: ATM CE + ATM PE >= this -> exit all, stopped for the day (0 = off)
        "exit_below": 0.0,       # owner 17-Nov: ATM CE + ATM PE <= this -> exit all, stopped for the day (0 = off)
        "itm_push": 1,           # owner 18-Nov (NIFTY/SENSEX flex): a sold strike ITM vs the buffered ATM -> push back now
        "profit_target": 0.0,    # owner 18-Nov: MTM >= this (rupees) -> exit all, stopped for the day (0 = off)
        "lock_start": 0.0,       # owner 18-Nov: trailing profit lock - once the day's best MTM reaches this ...
        "lock_gap": 0.0,         # ... exit all when MTM falls this far below that best (0 = off)
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

    @classmethod
    def unit(cls, ctx) -> int:
        """Lots per strike. An open book keeps the size it was entered with: 11-Nov SENSEX was changed
        1 -> 10 lots mid-day and the next A would have bought 10 of a 1-lot short (net long 9).
        The `lots` setting applies to the next fresh entry."""
        want = max(1, int(ctx.params.get("lots") or 1))
        legs = cls.legs(ctx)
        ce, pe = sum(legs["CE"].values()), sum(legs["PE"].values())
        if ce and ce == pe and ce % 3 == 0:
            return ce // 3
        return want

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
    def paired(ops, atm=None):
        """Owner 17-Nov (09:15 entry sat on CALLS ONLY for 10 s, MTM -8,400): orders of one direction
        (all SELL at entry, all BUY / all SELL on a resize) go out as CE / PE PAIRS - call, then put of the
        same number of lots - nearest the money first. After every pair the calls and puts done are equal,
        and the next pair is only sent once both legs of this one have filled (orders run one by one and
        the first failure stops the rest). Lots are split where needed so each pair has equal lots."""
        def near(o):
            return (abs(o[1] - atm) if atm is not None else 0, o[1])
        side = {k: [list(o) for o in sorted((o for o in ops if o[0] == k), key=near)] for k in ("CE", "PE")}
        out, ce, pe = [], side["CE"], side["PE"]
        while ce and pe:
            n = min(ce[0][3], pe[0][3])
            out += [(ce[0][0], ce[0][1], ce[0][2], n), (pe[0][0], pe[0][1], pe[0][2], n)]
            for q in (ce, pe):
                q[0][3] -= n
                if q[0][3] <= 0:
                    q.pop(0)
        return out + [tuple(o) for o in ce + pe]                   # unequal totals: the rest last

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

    def atm_straddle(self, ctx, atm):
        """ATM CE + ATM PE price (LTP kept inside bid / ask), or None while either is missing."""
        ce, pe = ctx.contract(atm, "CE"), ctx.contract(atm, "PE")
        a, b = self.price(ctx, ce), self.price(ctx, pe)
        return round(a + b, 2) if a > 0 and b > 0 else None

    def level_exit(self, ctx, atm) -> bool:
        """Owner 17-Nov: exit everything the first tick ATM CE+PE trades at / above `exit_above` or at / below
        `exit_below` (0 = off; both editable in the market). Stopped for the day, like the MTM stop."""
        cp = self.atm_straddle(ctx, atm)
        ctx.state["atm_cp"] = cp
        hi, lo = float(ctx.params.get("exit_above") or 0), float(ctx.params.get("exit_below") or 0)
        if cp is None or not ((hi > 0 and cp >= hi) or (lo > 0 and cp <= lo)):
            return False
        why = (f"ATM {atm:g} CE+PE {cp:g} >= exit level {hi:g}" if hi > 0 and cp >= hi
               else f"ATM {atm:g} CE+PE {cp:g} <= exit level {lo:g}")
        out = ctx.book.exit_all("level exit: " + why)
        ctx.state["phase"] = "stopped"
        ctx.state["blocked"] = f"EXITED: {why} - stopped for the day (Start to trade again)"
        ctx.log("level_exit", why=why, mtm=ctx.book.mtm()["total"], exits=len(out))
        ctx.save()
        return True

    def money_exit(self, ctx) -> bool:
        """Owner 18-Nov: profit target + trailing profit lock, both on the day's MTM (booked + open), both
        editable in the market (0 = off), first tick, stopped for the day like the MTM stop. A (re)Start sets
        `money_base`: a target already met at the Start does not fire at once, and the lock's best restarts."""
        m = float(ctx.book.mtm()["total"])
        base = ctx.state.get("money_base")
        peak = max(float(ctx.state.get("mtm_peak") if ctx.state.get("mtm_peak") is not None else m), m)
        ctx.state["mtm_peak"] = peak
        tgt = float(ctx.params.get("profit_target") or 0)
        ls, gap = float(ctx.params.get("lock_start") or 0), float(ctx.params.get("lock_gap") or 0)
        ctx.state["lock_floor"] = (peak - gap) if ls > 0 and gap > 0 and peak >= ls else None
        why = None
        if tgt > 0 and m >= tgt and not (base is not None and base >= tgt):
            why = f"profit target: MTM {m:,.0f} >= {tgt:,.0f}"
        elif ctx.state["lock_floor"] is not None and m <= ctx.state["lock_floor"]:
            why = f"profit lock: MTM {m:,.0f} fell {gap:,.0f} below the day's best {peak:,.0f}"
        if not why:
            return False
        out = ctx.book.exit_all(why)
        ctx.state["phase"] = "stopped"
        ctx.state["blocked"] = f"EXITED: {why} - stopped for the day (Start to trade again)"
        ctx.log("profit_exit", why=why, mtm=ctx.book.mtm()["total"], exits=len(out))
        ctx.save()
        return True

    def resize_target(self, ctx, legs, new):
        """Owner 17-Nov: lots changed while in trade -> every strike scaled by new/old, same shape (1a / 2a).
        Leftover lots (when a strike's share is not whole) go, one at a time, to the strike that keeps the
        imbalance nearest 0 - never more than its rounded-up share, so a cut only buys and a raise only sells."""
        import math
        ce, pe = sum(legs["CE"].values()), sum(legs["PE"].values())
        if not ce or ce != pe or ce % 3:
            return None, f"sides hold {ce} / {pe} lots - not a whole {ce // 3 or 1}-lot book"
        old = ce // 3
        if new == old:
            return None, "same size"
        r = new / old
        tgt = {k: {s: int(math.floor(v * r + 1e-9)) for s, v in legs[k].items()} for k in ("CE", "PE")}
        cap = {k: {s: int(math.ceil(v * r - 1e-9)) for s, v in legs[k].items()} for k in ("CE", "PE")}
        for _ in range(3 * new):
            moved = False
            for k in ("CE", "PE"):
                if sum(tgt[k].values()) >= 3 * new:
                    continue
                best = None
                for st_ in tgt[k]:
                    if tgt[k][st_] >= cap[k][st_]:
                        continue
                    trial = {"CE": dict(tgt["CE"]), "PE": dict(tgt["PE"])}
                    trial[k][st_] += 1
                    im = self.plan_imbalance(ctx, {x: {a: b for a, b in trial[x].items() if b} for x in trial})
                    score = abs(im) if im is not None else 1e9
                    if best is None or score < best[0]:
                        best = (score, st_)
                if best:
                    tgt[k][best[1]] += 1
                    moved = True
            if not moved:
                break
        tgt = {k: {s: n for s, n in v.items() if n} for k, v in tgt.items()}
        if sum(tgt["CE"].values()) != 3 * new or sum(tgt["PE"].values()) != 3 * new:
            return None, "could not split the lots evenly"
        return tgt, old

    def resize(self, ctx, new, atm):
        """Run a pending lots change on the open book. True = done / dropped, False = try again next tick."""
        legs = self.legs(ctx)
        target, old = self.resize_target(ctx, legs, new)
        if target is None:
            ctx.state["resize_to"] = None
            if old != "same size":
                ctx.log("resize_skipped", why=old)
            return True
        ops = self.paired(self.op_to(legs, target), atm)
        cost = self.cost_note(ctx, ops) if hasattr(self, "cost_note") else f"{len(ops)} orders"
        why = f"resize {old} -> {new} lots ({'buy back' if new < old else 'add'} on every strike, {cost})"
        self._fail = None
        if self.execute(ctx, ops, why):
            ctx.state["resize_to"] = None
            return True
        if self._fail == "order":                                       # live: the book's guard halts it
            ctx.state["resize_to"] = None
        return False

    def entry(self, ctx, atm):
        step, unit, band = self.step(ctx), self.unit(ctx), float(ctx.params["band"])
        rq = ctx.quote(ctx.market.ref_key(ctx.name, ctx.expiry))
        if rq and rq.recv_ts and time.time() - rq.recv_ts > 60:          # no entry on a stale price (holiday)
            return self.blocked(ctx, "entry: no live price in the last 60 s (market closed?)")
        legs, notes = self.plan_book(ctx, atm)
        if legs is None:
            return self.blocked(ctx, f"entry: {notes}", key="entry prices")
        ops = self.paired([(k, s, "SELL", n) for k in ("CE", "PE") for s, n in sorted(legs[k].items())], atm)
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
        im = ctx.book.imbalance()
        imb = im["pct"]
        if imb is None:
            if im.get("missing"):
                self.blocked(ctx, "waiting for prices of " + ", ".join(im["missing"]), key="missing prices")
            return
        # BAND-ONLY (owner 9-Nov): inside +-band nothing trades, even if ATM moved, a leg is ITM or a
        # strike is doubled. On a breach: go normal -> re-centre -> ITM leg back -> light side to ATM -> A/B/C.
        if abs(imb) <= band:
            # Optional early clean-up (normal_trigger < band): only "back to normal", nothing else.
            trig = float(ctx.params.get("normal_trigger") or band)
            if abs(imb) > trig:
                ops = self.op_go_normal(legs, atm, step, unit)
                if ops:
                    after = self.plan_imbalance(ctx, self.apply_ops(legs, ops))
                    if after is not None and abs(after) <= float(ctx.params["normal_band"]):
                        yield ops, f"back to normal early ({imb:+.1f}% -> clean {after:+.1f}%)"
            return
        heavy = "CE" if imb > 0 else "PE"
        light = "PE" if heavy == "CE" else "CE"
        # 1. GO NORMAL: the clean ATM/+-1/+-2 set, priced now, inside +-normal_band (only differing strikes trade).
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
        found = False
        # 3. ITM LEG BACK FIRST (owner 9-Nov): a fresh ITM leg on the heavy side is rolled out (= A on it).
        a_ops = self.op_A(legs, heavy, atm, step, unit)
        side = legs[heavy]
        itm = side and (min(side) < atm if heavy == "CE" else max(side) > atm)
        if a_ops and itm:
            found = True
            yield a_ops, f"ITM back ({heavy} heavy {imb:+.1f}%)"
        # 4. LIGHT SIDE TO ATM (owner 9-Nov): sell the ATM strike before doubling a held one (B).
        roll = self.op_atm(legs, light, atm, unit) if legs[light] else None
        if roll:
            found = True
            yield roll, f"roll {light} to ATM ({heavy} heavy {imb:+.1f}%)"
        for tag, ops in (("A", None if itm else a_ops),
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

    # ── adjustment-loop guard (17-Nov-2026) ─────────────────────────────────
    # Crude expiry day: C -> ITM back -> B -> C ... returned the book to the SAME legs every 3 moves
    # (imbalance +15 -> -27 -> +32 -> +15: the deep-ITM 8200 PE at 142 vs OTM legs at 3.5 / 28 made every
    # one-lot move overshoot the band). It ran on every tick: 60,133 fills on CRUDEOIL (-6 L) and 247,801 on
    # CRUDEOIL_ST (-26 L) of pure spread. Now a move that would put the book back to legs it held in the
    # last LOOP_WINDOW_S seconds is skipped (the book waits for prices to move), and a book never makes
    # more than MAX_ADJ_PER_MIN adjustments in one minute.
    LOOP_WINDOW_S = 120
    MAX_ADJ_PER_MIN = 20

    @staticmethod
    def _legs_sig(legs) -> str:
        return "|".join(f"{k}:" + ",".join(f"{s:g}x{n}" for s, n in sorted(legs[k].items()) if n) for k in ("CE", "PE"))

    def _book_hist(self, ctx, now):
        hist = [h for h in (ctx.state.get("book_hist") or []) if now - h[0] < self.LOOP_WINDOW_S]
        cur = self._legs_sig(self.legs(ctx))
        if not hist or hist[-1][1] != cur:
            hist.append([now, cur])
        ctx.state["book_hist"] = hist[-100:]
        return hist

    def loop_ok(self, ctx, ops, reason) -> bool:
        now = time.time()
        m = int(now // 60)
        cnt = ctx.state.get("adj_minute") or [m, 0]
        if cnt[0] == m and cnt[1] >= self.MAX_ADJ_PER_MIN:
            self.blocked(ctx, f"{reason}: held - {self.MAX_ADJ_PER_MIN} adjustments already this minute (loop guard)",
                         key="loop guard: per-minute cap")
            return False
        if not self.closer_ok(ctx, ops, reason):
            return False
        hist = self._book_hist(ctx, now)
        after = self._legs_sig(self.apply_ops(self.legs(ctx), ops))
        seen = next((h for h in hist[:-1] if h[1] == after), None)
        if seen:
            ago = now - seen[0]
            self.blocked(ctx, f"{reason}: held - it would put the book back to its legs of {ago:.0f} s ago "
                              f"(adjustment loop guard)", key="loop guard: repeat book")
            if not ctx.state.get("loop_logged") or now - ctx.state["loop_logged"] > 300:
                ctx.state["loop_logged"] = now
                ctx.log("loop_guard", action=reason, back_to=after, seconds_ago=round(ago, 1))
            return False
        return True

    def closer_ok(self, ctx, ops, reason) -> bool:
        """Owner 17-Nov-2026 (PAPER books only): outside the band, an adjustment must bring the imbalance
        CLOSER TO 0 than it is now. Crude expiry day: C (-15% -> +44%) and "ITM back" (+44% -> -45%) undid
        each other every 2 minutes from 17:28 (240 fills in 5 h, P&L unchanged, brokerage never counted).
        A move that lands further away is held; the book waits for prices to move. Live books unchanged."""
        r = getattr(ctx, "_r", None)
        if C.is_live(str(getattr(r, "name", "") or "")):
            return True
        band = float(ctx.params["band"])
        legs = self.legs(ctx)
        before = self.plan_imbalance(ctx, legs)
        if before is None or abs(before) <= band:
            return True
        after = self.plan_imbalance(ctx, self.apply_ops(legs, ops))
        if after is None or abs(after) < abs(before):
            return True
        self.blocked(ctx, f"{reason}: held - it would take the imbalance from {before:+.1f}% to {after:+.1f}% "
                          f"(further from 0)", key="closer rule")
        return False

    def loop_note(self, ctx):
        now = time.time()
        m = int(now // 60)
        cnt = ctx.state.get("adj_minute") or [m, 0]
        ctx.state["adj_minute"] = [m, cnt[1] + 1] if cnt[0] == m else [m, 1]
        self._book_hist(ctx, now)

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
        if ph == "in" and self.money_exit(ctx):
            return
        atm = self.current_atm(ctx)
        if atm is None:
            return
        if ph == "in" and self.level_exit(ctx, atm):
            return
        if ph == "wait_entry":
            self.entry(ctx, atm)
            ctx.save()
            return
        if ctx.state.get("resize_to"):                                  # owner 17-Nov: lots changed in trade
            self.resize(ctx, int(ctx.state["resize_to"]), atm)
            ctx.save()
            return
        acted = False
        self._blk = False
        for _ in range(int(ctx.params.get("max_actions", 8))):
            done = False
            for ops, reason in self.candidates(ctx, atm):          # held by a wide quote -> try the next
                self._fail = None
                if not self.loop_ok(ctx, ops, reason):
                    continue
                if self.execute(ctx, ops, reason):
                    self.loop_note(ctx)
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


class CePeSupertrend(CePeImbalance):
    """Supertrend VARIANT (owner 13-Nov-2026): the same CE/PE imbalance book, but only while Supertrend
    on the 1-minute ATM CE+PE candles is RED (premium falling). Signal: st_signal.StSignal via ctx.signal().
      * candle closes green while in  -> buy back everything, flat (no adjustments while flat)
      * flat and the last closed candle is red -> fresh entry (normal entry rules), then normal adjustments
      * opening rule: Supertrend already red at the open (after the previous-session warm-up) and no green
        candle yet today -> enter only once CE+PE trades below the LOW of the market's first 1-min candle
    A manual Start while red enters at once (same as a red close). MTM stop / session end as normal."""
    params = {**CePeImbalance.params, "st_period": 10, "st_mult": 3.0}

    def signal(self, ctx) -> dict:
        fn = getattr(ctx, "signal", None)
        s = fn(int(ctx.params.get("st_period") or 10), float(ctx.params.get("st_mult") or 3.0)) if fn else None
        s = s or {"ready": False}
        ctx.state["st"] = {k: s.get(k) for k in ("ready", "trend", "st", "px", "start_red", "flipped_today",
                                                  "first_low", "warm_source", "bars_today", "atm", "expiry")}
        return s

    def on_start(self, ctx):
        has = any(p.lots for p in ctx.book.positions.values())
        ctx.state["phase"] = "in" if has else "flat"
        ctx.state["blocked"] = None
        ctx.log("phase", phase=ctx.state["phase"])
        self.evaluate(ctx)

    def _evaluate(self, ctx):
        ph = ctx.state.get("phase")
        if ph in ("stopped", "done", None) or not ctx.in_session():
            return
        stop = float(ctx.params.get("mtm_stop") or 0)
        if stop > 0 and ctx.book.mtm()["total"] <= -stop:              # also counts booked losses while flat
            out = ctx.book.exit_all("MTM stop loss")
            ctx.state["phase"] = "stopped"
            ctx.log("stop_loss", mtm=ctx.book.mtm()["total"], exits=len(out))
            ctx.save()
            return
        if self.money_exit(ctx):                                        # day's MTM, booked too (like the stop)
            return
        s = self.signal(ctx)
        has = any(p.lots for p in ctx.book.positions.values())
        if has:
            if s.get("ready") and s.get("trend") == "green":
                out = ctx.book.exit_all("Supertrend green")
                ctx.state["phase"] = "flat"
                ctx.state["blocked"] = ctx.state["blocked_key"] = None
                ctx.log("adjust", action=f"exit: Supertrend green (CE+PE {s.get('px')} > ST {s.get('st')})",
                        imb_before=None, imb_after=None, atm=ctx.state.get("atm"),
                        fills=[f"{t.get('side')} {t.get('lots')} {t.get('symbol')} @{t.get('price')}" for t in out])
                ctx.save()
                return
            ctx.state["phase"] = "in"
            return super()._evaluate(ctx)
        ctx.state["phase"] = "flat"
        if not s.get("ready") or s.get("trend") != "red":
            return
        if s.get("start_red") and not s.get("flipped_today"):
            low, px = s.get("first_low"), s.get("px")
            if low is None or px is None or not px < low:
                return self.blocked(ctx, f"red since the open: waiting for CE+PE below the first candle's low "
                                         f"{low if low is not None else '–'} (now {px if px is not None else '–'})",
                                    key="st opening low")
            why = f"opening low break (CE+PE {px} < {low})"
        else:
            why = f"Supertrend red (CE+PE {s.get('px')} < ST {s.get('st')})"
        ctx.state["phase"] = "wait_entry"
        n = len(ctx.book.trades)
        super()._evaluate(ctx)                                           # normal entry rules
        if ctx.state.get("phase") == "in" and len(ctx.book.trades) > n:
            ctx.log("st_entry", why=why)
        elif ctx.state.get("phase") == "wait_entry":
            ctx.state["phase"] = "flat"                                  # entry held (wide quote...): retry


class CePeNewRules(CePeImbalance):
    """NEW RULES (owner 15/16-Nov-2026) — paper books "{NAME}_NR" / "{NAME}_NR_ST", band 30 % / step 400:
      * react ONLY on a band breach (no early 'back to normal' between normal_trigger and the band)
      * balance by making the HEAVY side lighter: its FRONT strike (nearest the money, ATM included) is
        bought back and sold again beyond its LAST strike (A), repeated while beyond the band. Near expiry
        (e.g. ATM CE 30 vs ATM PE 150) that leaves BOTH sides OTM - the light side is never pulled in.
      * NO C (never sell ITM). Entry / re-centre plan the same way: clean set, then A on the heavy side.
      * WAIT FOR A: a wide / missing quote on A's strikes holds the book (no fall-through to B).
        A only fires when it lowers |imbalance| (no CE<->PE ping-pong). B only when A cannot be built.
      * every side keeps its lots on at least 2 different strikes (3 strikes never balanced by 1).
      * a leg that drifts ITM is rolled out only on a breach (A takes the front strike first)."""

    params = {**CePeImbalance.params, "band": 30.0, "step": 400}
    MIN_STRIKES = 2

    def op_A_any(self, legs, heavy, step, unit):
        side = legs[heavy]
        if not side:
            return None
        if heavy == "CE":
            first, target = min(side), max(side) + step
        else:
            first, target = max(side), min(side) - step
        return [(heavy, first, "BUY", unit), (heavy, target, "SELL", unit)]

    def strikes_ok(self, old, new):
        """Each side on >= MIN_STRIKES strikes (a side already below that may not lose more)."""
        return all(len(new[k]) >= min(self.MIN_STRIKES, max(len(old[k]), 1)) or not new[k] for k in ("CE", "PE"))

    def plan_book(self, ctx, atm):
        """Clean set at `atm`, then A on the heavy side until |imbalance| <= band (or A stops helping)."""
        step, unit, band = self.step(ctx), self.unit(ctx), float(ctx.params["band"])
        legs = {"CE": self.clean("CE", atm, step, unit), "PE": self.clean("PE", atm, step, unit)}
        imb = self.plan_imbalance(ctx, legs)
        if imb is None:
            return None, "waiting for prices of the 6 strikes"
        notes = []
        for _ in range(6):
            if abs(imb) <= band:
                break
            heavy = "CE" if imb > 0 else "PE"
            ops = self.op_A_any(legs, heavy, step, unit)
            if not ops or not ctx.contract(ops[1][1], heavy):
                break                                              # no strike that far out
            nxt_legs = self.apply_ops(legs, ops)
            nxt = self.plan_imbalance(ctx, nxt_legs)
            if nxt is None:
                return None, f"waiting for prices of {heavy} {ops[1][1]:g}"
            if abs(nxt) >= abs(imb):
                break
            legs, imb = nxt_legs, nxt
            notes.append(f"A {heavy} {ops[0][1]:g}->{ops[1][1]:g}")
        return legs, notes

    def candidates(self, ctx, atm):
        step, unit, band = self.step(ctx), self.unit(ctx), float(ctx.params["band"])
        legs = self.legs(ctx)
        im = ctx.book.imbalance()
        imb = im["pct"]
        if imb is None or abs(imb) <= band:
            return
        heavy = "CE" if imb > 0 else "PE"
        ops = self.op_go_normal(legs, atm, step, unit)
        if ops:
            after = self.plan_imbalance(ctx, self.apply_ops(legs, ops))
            if after is not None and abs(after) <= float(ctx.params["normal_band"]):
                yield ops, f"back to normal (clean set {after:+.1f}%)"
        clean = {k: self.clean(k, atm, step, unit) for k in ("CE", "PE")}
        if legs["CE"] != clean["CE"] and legs["PE"] != clean["PE"]                 and sum(legs["CE"].values()) == sum(legs["PE"].values()) == 3 * unit:
            target, notes = self.plan_book(ctx, atm)
            if target is not None and target != legs and (target["CE"] == clean["CE"] or target["PE"] == clean["PE"]):
                after = self.plan_imbalance(ctx, target)
                if after is not None and abs(after) <= band:
                    yield self.op_to(legs, target), f"re-centre ({after:+.1f}%)"
        a = self.op_A_any(legs, heavy, step, unit)
        if a and ctx.contract(a[1][1], heavy):
            nxt = self.apply_ops(legs, a)
            after = self.plan_imbalance(ctx, nxt)
            if after is None:
                self.blocked(ctx, f"A ({heavy} heavy {imb:+.1f}%): waiting for a price of {heavy} {a[1][1]:g}",
                             key=f"A price {heavy} {a[1][1]:g}")
                return
            if abs(after) >= abs(imb):
                self.blocked(ctx, f"A ({heavy} heavy {imb:+.1f}%) would not lower the imbalance ({after:+.1f}%): waiting",
                             key=f"A no gain {heavy}")
                return
            if self.strikes_ok(legs, nxt):
                yield a, f"A front->back ({heavy} heavy {imb:+.1f}%)"
                return                                                   # held by a wide quote: wait for A, never B
        b = self.op_B(legs, heavy, atm, unit)                            # A cannot be built at all
        if b and self.strikes_ok(legs, self.apply_ops(legs, b)):
            yield b, f"B ({heavy} heavy {imb:+.1f}%)"


class CePeSupertrendNewRules(CePeSupertrend, CePeNewRules):
    """Owner 16-Nov-2026 — book "{NAME}_NR_ST": Supertrend decides in / flat (CePeSupertrend), and while in,
    adjustments follow the new rules (CePeNewRules.candidates via the MRO)."""
    params = {**CePeNewRules.params, "st_period": 10, "st_mult": 3.0}


class CePeFlex(CePeImbalance):
    """FLEX RULES (owner 16/17-Nov-2026) - NIFTY / SENSEX normal, Supertrend and LIVE books.
    Inside the band: unchanged (only the early 'back to normal'). On a breach, in this order:
      back to normal -> re-centre -> ITM back -> light side back to ATM -> heavy shift -> B -> wait.
      * heavy shift: lots of the HEAVY side move OUT to just beyond its farthest strike (partial or full,
        from whichever strike fixes it best). At most 4 strikes a side; its front never past OTM2.
      * B: lots of the LIGHT side move IN to the next strike it already holds, never past ATM.
      * flexible lots: each step takes the FEWEST lots that bring the book inside the band (tie -> nearest
        0 %); if nothing gets inside, the step that lowers |imbalance| the most. Every step must lower it.
    Guards: every side on >= 2 strikes (3 strikes never balanced by 1) - at least one side keeps a strike at
    ATM (a light side without ATM comes straight back to ATM first) - NO C: nothing is ever SOLD in the money
    - the whole chain is planned on paper first and only the NET orders go out - each adjustment logs its
    order count and bid-ask cost."""
    MIN_STRIKES = 2
    MAX_STRIKES = 4
    FRONT_MAX = 2            # heavy side's nearest strike: at most this many steps OTM
    MAX_SPAN = 3             # 17-Nov fix: heavy side's farthest strike at most 3 steps beyond its nearest (the
                             # "4th strike"); before, 1-lot back legs were walked out to 26000 (worthless)
    MIN_GAIN = 0.5           # 17-Nov fix: a step must lower |imbalance| by at least 0.5 points (no creeping)
    MAX_STEPS = 12           # steps planned in one adjustment
    STALE_S = 5.0            # owner 17-Nov: index price older than this -> hold adjustments (stop / square-off still act)

    # ── helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def otm(kind, k, atm, step) -> int:
        """Steps out of the money (0 = ATM, negative = ITM)."""
        return int(round(((k - atm) if kind == "CE" else (atm - k)) / step))

    def _px(self, ctx, kind, k):
        key = (kind, k)
        if key not in self._pxc:
            c = ctx.contract(k, kind)
            self._pxc[key] = (self.price(ctx, c) * c.lot * c.mult) if c else 0.0
        return self._pxc[key]

    def _imb(self, ctx, legs):
        tot = {"CE": 0.0, "PE": 0.0}
        for kind in ("CE", "PE"):
            for k, n in legs[kind].items():
                px = self._px(ctx, kind, k)
                if px <= 0:
                    return None
                tot[kind] += px * n
        t = tot["CE"] + tot["PE"]
        return (tot["CE"] - tot["PE"]) / t * 100 if t > 0 else None

    def _sellable(self, ctx, kind, k, atm, step) -> bool:
        key = (kind, k)
        if key not in self._sellc:
            ok = self.otm(kind, k, atm, step) >= 0                      # never SELL in the money
            c = ctx.contract(k, kind) if ok else None
            self._sellc[key] = bool(c) and self._px(ctx, kind, k) > 0 and self.spread_ok(ctx, c)[0]
        return self._sellc[key]

    @staticmethod
    def gaps(side, step) -> int:
        """Empty strikes between a side's nearest and farthest strike (owner 19-Nov: NO GAPS)."""
        ks = sorted(k for k in side if (side[k] if isinstance(side, dict) else 1))
        return 0 if len(ks) < 2 else int(round((ks[-1] - ks[0]) / step)) + 1 - len(ks)

    def gaps_ok(self, old, new, step, atm=None) -> bool:
        """Owner 19-Nov-2026: strikes on a side stay next to each other - ALWAYS, also on the way back to ATM.
        A move may not leave either side with MORE empty strikes inside it than it has now (a book that already
        has a gap may still close it). Getting back to ATM without a gap = "to ATM" rebuilds that side as 3
        continuous strikes from ATM (moves_atm); ITM push-back already does the same.
        Replayed 13-19 Nov: gaps only ever formed on paper books (3 of 10 days), about break-even, fewer fills."""
        return all(self.gaps(new[k], step) <= self.gaps(old[k], step) for k in ("CE", "PE"))

    def guards_ok(self, old, new, atm, step, heavy=None) -> bool:
        if not self.gaps_ok(old, new, step, atm):
            return False                                                 # 19-Nov: no empty strike inside a side
        for k in ("CE", "PE"):
            if new[k] and len(new[k]) < min(self.MIN_STRIKES, max(len(old[k]), 1)):
                return False                                             # >= 2 strikes a side
            if len(new[k]) > max(self.MAX_STRIKES, len(old[k])):
                return False                                             # <= 4 strikes a side
        if (atm in old["CE"] or atm in old["PE"]) and not (atm in new["CE"] or atm in new["PE"]):
            return False                                                 # one side keeps ATM
        if heavy and new[heavy]:
            def front(s):
                return min(self.otm(heavy, k, atm, step) for k in s)

            if front(new[heavy]) > max(self.FRONT_MAX, front(old[heavy]) if old[heavy] else 0):
                return False                                             # front never past OTM2
        for k in ("CE", "PE"):                                           # 17-Nov: BOTH sides stay bunched
            o_new = [self.otm(k, x, atm, step) for x in new[k]]
            o_old = [self.otm(k, x, atm, step) for x in old[k]]
            sp_new = max(o_new) - min(o_new) if o_new else 0
            sp_old = max(o_old) - min(o_old) if o_old else 0
            if sp_new > max(self.MAX_SPAN, sp_old):
                return False                                             # last strike <= 3 steps from the first
        return True

    def loop_ok(self, ctx, ops, reason) -> bool:
        legs = self.legs(ctx)
        if not self.gaps_ok(legs, self.apply_ops(legs, ops), self.step(ctx), ctx.state.get("atm")):
            self.blocked(ctx, f"{reason}: held - it would leave an empty strike inside a side (no-gaps rule)",
                         key="no gaps")
            return False
        return super().loop_ok(ctx, ops, reason)

    # ── move builders ───────────────────────────────────────────────────────
    def moves_out(self, legs, heavy, atm, step, sources=None):
        """Heavy shift: n lots from strike s to just beyond the farthest strike (or onto the farthest)."""
        side = legs[heavy]
        if not side:
            return
        sgn = 1 if heavy == "CE" else -1
        far = max(side) if heavy == "CE" else min(side)
        for s in sorted(side, key=lambda k: self.otm(heavy, k, atm, step)):
            if sources is not None and s not in sources:
                continue
            for t in (far + sgn * step, far):
                if t == s:
                    continue
                for n in range(1, side[s] + 1):
                    yield [(heavy, s, "BUY", n), (heavy, t, "SELL", n)], f"{heavy} {s:g}->{t:g} x{n}"

    def moves_in(self, legs, heavy, atm, step):
        """B: n lots of the light side from strike s in to the next strike it already holds (never ITM)."""
        light = "PE" if heavy == "CE" else "CE"
        side = legs[light]
        order = sorted(side, key=lambda k: self.otm(light, k, atm, step))
        for i in range(1, len(order)):
            s, t = order[i], order[i - 1]
            if self.otm(light, t, atm, step) < 0:
                continue
            for n in range(1, side[s] + 1):
                yield [(light, s, "BUY", n), (light, t, "SELL", n)], f"{light} {s:g}->{t:g} x{n}"

    def moves_atm(self, legs, light, atm, step):
        """Light side has no ATM strike: its farthest strike comes straight back to ATM."""
        side = legs[light]
        if not side or atm in side:
            return
        far = max(side, key=lambda k: (abs(k - atm), k if light == "CE" else -k))
        for n in range(1, side[far] + 1):
            yield [(light, far, "BUY", n), (light, atm, "SELL", n)], f"{light} {far:g}->ATM {atm:g} x{n}"
        # owner 19-Nov (no gaps): when the jump above would leave an empty strike, the side comes back to ATM as
        # 3 continuous strikes ATM / OTM1 / OTM2 with the same lots (net orders only), like ITM push-back
        n = sum(side.values())
        sgn = 1 if light == "CE" else -1
        ladder = [atm + sgn * i * step for i in range(3)]
        out = {k: n // 3 for k in ladder}
        for k in ladder[:n % 3]:
            out[k] += 1
        new = {"CE": dict(legs["CE"]), "PE": dict(legs["PE"])}
        new[light] = {k: v for k, v in out.items() if v}
        ops = self.op_to(legs, new)
        if ops and new[light] != side:
            yield ops, f"{light} to ATM as {','.join(f'{k:g}' for k in ladder)}"

    def best_move(self, ctx, legs, cands, imb, band, atm, step, heavy=None):
        best = None
        for ops, tag in cands:
            if not all(self._sellable(ctx, o[0], o[1], atm, step) for o in ops if o[2] == "SELL"):
                continue
            new = self.apply_ops(legs, ops)
            if not self.guards_ok(legs, new, atm, step, heavy):
                continue
            after = self._imb(ctx, new)
            if after is None or abs(after) > abs(imb) - self.MIN_GAIN:   # must lower |imbalance| (>= 0.5 pt)
                continue
            n = ops[0][3]
            rank = (0, n, abs(after)) if abs(after) <= band else (1, abs(after), n)
            if best is None or rank < best[0]:
                best = (rank, tag, new, after)
        return best

    def chain(self, ctx, legs, atm, imb, band):
        """Plan every step on paper. Returns (final legs, final imbalance, step notes)."""
        step = self.step(ctx)
        self._pxc, self._sellc = {}, {}
        cur, ci, notes = legs, imb, []
        for _ in range(self.MAX_STEPS):
            if ci is None or abs(ci) <= band:
                break
            heavy = "CE" if ci > 0 else "PE"
            light = "PE" if heavy == "CE" else "CE"
            itm = {k for k in cur[heavy] if self.otm(heavy, k, atm, step) < 0}
            pick = phase = None
            for phase, gen, hv in (("ITM back", self.moves_out(cur, heavy, atm, step, itm) if itm else (), heavy),
                                   ("to ATM", self.moves_atm(cur, light, atm, step), None),
                                   ("shift", self.moves_out(cur, heavy, atm, step), heavy),
                                   ("B", self.moves_in(cur, heavy, atm, step), None)):
                pick = self.best_move(ctx, cur, gen, ci, band, atm, step, hv)
                if pick:
                    break
            if not pick:
                break
            _, tag, cur, ci = pick
            notes.append(f"{phase} {tag}")
        return cur, ci, notes

    def cost_note(self, ctx, ops) -> str:
        tot = 0.0
        for kind, k, _side, n in ops:
            c = ctx.contract(k, kind)
            q = ctx.quote(c.key) if c else None
            if q and q.bid and q.ask and q.ask >= q.bid:
                tot += (q.ask - q.bid) / 2 * n * c.lot * c.mult
        return f"{len(ops)} orders, bid-ask ~{tot:,.0f}"

    # ── rules ───────────────────────────────────────────────────────────────
    def plan_book(self, ctx, atm):
        """Entry / re-centre: clean set at `atm`, then the same chain (heavy shift first, then B; no C)."""
        step, unit, band = self.step(ctx), self.unit(ctx), float(ctx.params["band"])
        legs = {"CE": self.clean("CE", atm, step, unit), "PE": self.clean("PE", atm, step, unit)}
        imb = self.plan_imbalance(ctx, legs)
        if imb is None:
            return None, "waiting for prices of the 6 strikes"
        final, _after, notes = self.chain(ctx, legs, atm, imb, band)
        return final, notes

    def push_back(self, ctx, legs, atm, step, band):
        """Owner 18-Nov: a SOLD strike that is in the money against the BUFFERED ATM (ctx.state['atm'] - it only
        moves 50 % + atm_buffer_pct of a step past the half-way point) is pushed back at once, band or not:
        that side is rebuilt as ATM / OTM1 / OTM2 with the same lots (net orders only); if the book is then
        outside the band, the flex chain rebalances it in the same plan. Returns (ops, reason) or None."""
        new, moved = {k: dict(v) for k, v in legs.items()}, []
        for kind in ("CE", "PE"):
            side = legs[kind]
            itm = sorted(k for k in side if self.otm(kind, k, atm, step) < 0)
            if not itm:
                continue
            n = sum(side.values())
            sgn = 1 if kind == "CE" else -1
            ladder = [atm + sgn * i * step for i in range(3)]
            out = {k: n // 3 for k in ladder}
            for k in ladder[:n % 3]:
                out[k] += 1
            new[kind] = {k: v for k, v in out.items() if v}
            moved.append(f"{kind} {','.join(f'{k:g}' for k in itm)} ITM")
        if not moved:
            return None
        self._pxc, self._sellc = {}, {}
        for kind in ("CE", "PE"):
            for k, v in new[kind].items():
                if v > legs[kind].get(k, 0) and not self._sellable(ctx, kind, k, atm, step):
                    self.blocked(ctx, f"ITM push-back ({'; '.join(moved)}): {kind} {k:g} has no fair quote yet",
                                 key="itm push: wide quote")
                    return None
        after = self._imb(ctx, new)
        notes = []
        if after is not None and abs(after) > band:
            new, after, notes = self.chain(ctx, new, atm, after, band)
        ops = self.op_to(legs, new)
        if not ops:
            return None
        return ops, (f"ITM push-back ({'; '.join(moved)} vs ATM {atm:g} -> "
                     f"{'+' if (after or 0) >= 0 else ''}{(after or 0):.1f}%"
                     f"{': ' + '; '.join(notes) if notes else ''}; {self.cost_note(ctx, ops)})")

    def candidates(self, ctx, atm):
        step, unit, band = self.step(ctx), self.unit(ctx), float(ctx.params["band"])
        legs = self.legs(ctx)
        imb = ctx.book.imbalance()["pct"]
        rq = ctx.quote(ctx.market.ref_key(ctx.name, ctx.expiry))
        age = time.time() - rq.recv_ts if rq and rq.recv_ts else 0.0
        if age > self.STALE_S:                                           # feed dropped / lagging: never adjust blind
            ctx.state["stale_s"] = round(age, 1)
            self.blocked(ctx, f"prices stale {age:.0f} s - adjustments held until the feed is back", key="stale prices")
            return
        ctx.state["stale_s"] = None
        if int(ctx.params.get("itm_push", 1) or 0):                     # 0. ITM PUSH-BACK (owner 18-Nov), band or not
            pb = self.push_back(ctx, legs, atm, step, band)
            if pb:
                yield pb
                return
        if imb is None or abs(imb) <= band:
            yield from super().candidates(ctx, atm)                     # missing prices / early normal only
            return
        heavy = "CE" if imb > 0 else "PE"
        # 1. BACK TO NORMAL: clean set now within +-normal_band.
        ops = self.op_go_normal(legs, atm, step, unit)
        if ops:
            after = self.plan_imbalance(ctx, self.apply_ops(legs, ops))
            if after is not None and abs(after) <= float(ctx.params["normal_band"]) and abs(after) < abs(imb):
                yield ops, f"back to normal (clean set {after:+.1f}%, {self.cost_note(ctx, ops)})"
        # 2. RE-CENTRE: both sides adjusted -> rebuild like entry when that plan is inside the band.
        clean = {k: self.clean(k, atm, step, unit) for k in ("CE", "PE")}
        if legs["CE"] != clean["CE"] and legs["PE"] != clean["PE"] \
                and sum(legs["CE"].values()) == sum(legs["PE"].values()) == 3 * unit:
            target, _notes = self.plan_book(ctx, atm)
            if target is not None and target != legs:
                after = self.plan_imbalance(ctx, target)
                if after is not None and abs(after) <= band:
                    ops = self.op_to(legs, target)
                    yield ops, f"re-centre ({after:+.1f}%, {self.cost_note(ctx, ops)})"
        # 3. THE CHAIN: ITM back -> light to ATM -> heavy shift -> B, planned ahead, net orders only.
        final, after, notes = self.chain(ctx, legs, atm, imb, band)
        ops = self.op_to(legs, final) if notes else None
        if ops:
            yield ops, (f"flex ({heavy} heavy {imb:+.1f}% -> {after:+.1f}%: {'; '.join(notes)}; "
                        f"{self.cost_note(ctx, ops)})")
        else:
            self.blocked(ctx, f"{heavy} heavy {imb:+.1f}%: no move allowed (2-strike / ATM / OTM2 rules or wide quotes)",
                         key=f"{heavy} heavy: no flex move")


class CePeSupertrendFlex(CePeSupertrend, CePeFlex):
    """Supertrend in / flat (CePeSupertrend) + the flex adjustment rules while in (NIFTY_ST / SENSEX_ST)."""
    params = {**CePeSupertrend.params}


REGISTRY = {"cepe": CePeImbalance, "cepe_st": CePeSupertrend, "cepe_new": CePeNewRules,
            "cepe_st_new": CePeSupertrendNewRules,
            "cepe_flex": CePeFlex, "cepe_st_flex": CePeSupertrendFlex, "idle": Idle}
DEFAULT = "cepe"
