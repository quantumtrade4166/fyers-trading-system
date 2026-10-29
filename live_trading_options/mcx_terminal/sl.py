"""
mcx_terminal/sl.py — per-position AUTO STOP-LOSS + TRAIL (owner's rules, 29-Oct-2026).
====================================================================================

The row on the main screen (saved in settings.json as "auto_sl"):
  AUTO ARM ON   sell | buy | all positions
  STOP LOSS     [Auto arm]  value  % | pts   QTY full | half   ON HIT exit   (held AT BROKER)
  TRAIL SL      [Auto arm]  [On]  every N   (N in the same unit as the stop-loss)

LIVE: each protected option position gets a REAL stop-loss-limit order at Kotak (order type "SL",
trigger + limit), so it still works if the terminal is down. Tag prefix TAG_SL ("mcxs") keeps these
orders out of the order book's own tracking.
  short  -> BUY  SL, trigger = entry x (1 + v%)  or entry + v pts
  long   -> SELL SL, trigger = entry x (1 - v%)  or entry - v pts
  limit  = trigger +/- LIMIT_PCT, so it fills once triggered; still open CHASE_S after trigger -> chased
  trail  : each `every` the price moves your way (from entry), the trigger moves `every` the same way

SAFETY (an SL order left resting after you exit would OPEN a new opposite position):
  * every exit the terminal sends first cancels that contract's SL order (before_reduce hook);
    if Kotak says the SL already FILLED, the exit is not sent
  * position flat / flipped at Kotak (hand exits in the Kotak app too) -> the SL order is cancelled
  * EXIT ALL cancels every working MCX order; re-arming pauses meanwhile
  * a stray "mcxs" order at Kotak with no position behind it is cancelled
  * Kotak refuses the SL order -> the terminal WATCHES the price and exits itself (said on screen)
  * the stop already crossed when the position is found -> NOT placed, shown red (never a surprise exit)
PAPER: same rules, the terminal watches the price (no broker).
"""
import datetime as dt
import json
import logging
import math
import threading
import time

from live_trading_options.mcx_terminal import audit
from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.sl")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

TAG_SL = "mcxs"
SEG = "mcx_fo"
LIMIT_PCT = 5.0            # SL limit price this far beyond the trigger
CHASE_S = 2.0              # triggered but not filled this long -> limit moved through the book
LOOP_S = 0.5
STATUS_EVERY_S = 1.0       # live: read Kotak's order book this often while SL orders exist
MODIFY_EVERY_S = 1.0       # at most one trail modify per position per second
REARM_PAUSE_S = 4.0        # after an exit of a contract, wait this long before re-arming it

DEFAULTS = {"arm_on": "sell", "sl_auto": False, "sl_val": 50.0, "sl_unit": "%", "qty": "full",
            "on_hit": "exit", "trail_auto": False, "trail_on": False, "trail_every": 10.0, "trail_move": 0.0}

BROKER_FILLED = ("complete", "traded")
BROKER_DEAD = ("rejected", "cancelled")


def clean(cfg) -> dict:
    """Validate the screen's auto_sl values -> full dict (raises ValueError on nonsense)."""
    v = dict(DEFAULTS)
    cfg = cfg or {}
    if cfg.get("arm_on") in ("sell", "buy", "all"):
        v["arm_on"] = cfg["arm_on"]
    if cfg.get("sl_unit") in ("%", "pts"):
        v["sl_unit"] = cfg["sl_unit"]
    if cfg.get("qty") in ("full", "half"):
        v["qty"] = cfg["qty"]
    for k in ("sl_auto", "trail_auto", "trail_on"):
        if k in cfg:
            v[k] = bool(cfg[k])
    for k in ("sl_val", "trail_every", "trail_move"):
        if k in cfg and cfg[k] not in (None, ""):
            f = float(cfg[k])
            if not (f > 0 or (k == "trail_move" and f == 0)):      # trail_move 0 = move as far as `every`
                raise ValueError(f"{k} must be above 0")
            v[k] = f
    if v["sl_unit"] == "%" and v["sl_val"] > 500:
        raise ValueError("stop-loss over 500 %")
    return v


def _rt(px, tick, up):
    if not tick:
        return round(px, 2)
    n = px / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 2)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class AutoSL:
    def __init__(self, book, ltp_of, settings, live: bool, path=None, on_change=None, notify=None, client=None):
        """book: PaperBook / LiveBook · ltp_of(token) · settings(): the settings dict (has "auto_sl")."""
        self.book = book
        self.u = book.u
        self.ltp_of = ltp_of
        self.settings = settings
        self.live = live
        self.on_change = on_change or (lambda: None)
        self.notify = notify or (lambda ev: None)
        self.path = path or (C.DATA / f"autosl_{'live' if live else 'paper'}_{dt.datetime.now(IST).date().isoformat()}.json")
        self.lock = threading.RLock()
        self.recs = {}            # token -> the stop-loss of that position
        self.done = set()         # tokens not to re-arm until flat (stop hit / dropped by the owner)
        self.memo = {}            # token -> trigger / best kept across a partial exit (trail progress)
        self.pause = {}           # token -> no re-arm before this time
        self.pause_all = 0.0
        self._seq = 0
        self._rows_at = 0.0
        self._kick = threading.Event()
        self._exits = []
        self._load()

    # ── state ──────────────────────────────────────────────────────────────
    def _load(self):
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                # only stops that sit at Kotak are kept across a restart; the rest are worked out again
                self.recs = {int(k): v for k, v in d.get("recs", {}).items() if v.get("oid")}
                self.done = set(int(t) for t in d.get("done", []))
                self.memo = {int(k): v for k, v in d.get("memo", {}).items()}
        except Exception:
            log.exception("auto SL state load failed")

    def _save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"recs": self.recs, "done": sorted(self.done), "memo": self.memo}, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            log.exception("auto SL state save failed")

    def cfg(self) -> dict:
        try:
            return clean((self.settings() or {}).get("auto_sl"))
        except Exception:
            return dict(DEFAULTS)

    def view(self) -> dict:
        with self.lock:
            return {str(t): {k: r.get(k) for k in ("trigger", "limit", "qty", "state", "msg", "trail", "entry", "at_broker",
                                                   "qty_mode")}
                    for t, r in self.recs.items()}

    # ── hooks from the book ────────────────────────────────────────────────
    def before_reduce(self, token: int) -> str | None:
        """The terminal is about to send an exit of `token`: take its SL order away FIRST.
        -> None (go ahead) or a message (do NOT send: the stop-loss already filled)."""
        with self.lock:
            self.pause[token] = time.time() + REARM_PAUSE_S
            r = self.recs.get(token)
            if r is None:
                return None
            if r.get("trigger") is not None:
                self.memo[token] = {"trigger": r["trigger"], "init": r.get("init"), "best": r.get("best"),
                                    "trail": r.get("trail"), "dir": r["side"]}
            oid = r.get("oid")
            if self.live and oid and r.get("state") not in ("hit",):
                err = self._cancel(oid)
                if err:
                    st = self._status_of(oid)
                    if st and any(w in st for w in BROKER_FILLED):
                        self._hit(token, r, "filled at Kotak")
                        return "stop-loss already FILLED at Kotak - exit not sent (check positions)"
                    log.warning("SL %s cancel refused before exit (%s) - status %s; exit goes ahead", oid, err, st)
            self.recs.pop(token, None)
            self._save()
        self.on_change()
        return None

    def before_flatten(self):
        with self.lock:
            self.pause_all = time.time() + 20.0

    def on_push(self, row: dict):
        """Kotak pushed an order update the order book did not know: maybe one of ours."""
        tag = str(row.get("tag") or row.get("ig") or "")
        oid = str(row.get("nOrdNo") or "")
        with self.lock:
            if tag.startswith(TAG_SL) or any(r.get("oid") == oid for r in self.recs.values()):
                self._rows_at = 0.0
                self._kick.set()

    def set_one(self, token: int, enabled: bool, trigger=None, qty=None) -> dict:
        """The SL cell of one Positions row: tick on/off, type a trigger price, Full / Half."""
        if not enabled:
            with self.lock:
                if token not in self.recs:
                    self.done.add(token)
                    self._save()
                    return {"ok": True, "msg": "no stop-loss on that position"}
            return self.drop(token)
        if self.live and not self.book.positions_fresh():
            return {"ok": False, "msg": "Kotak positions not read yet - try again in a moment"}
        cfg = self.cfg()
        with self.book.lock:
            p = dict(self.book.pos.get(token) or {})
        c = self.u.by_token.get(token)
        if not p.get("net") or c is None or c.kind not in ("CE", "PE"):
            return {"ok": False, "msg": "no open option position in that contract"}
        with self.lock:
            r = self.recs.get(token)
            if r is None:
                self.done.discard(token)
                self.pause.pop(token, None)
                self.memo.pop(token, None)
                self._arm(token, p, c, cfg)
                r = self.recs.get(token)
                if r is None:
                    return {"ok": False, "msg": "could not work out the entry price"}
            msg = []
            if qty in ("full", "half"):
                r["qty_mode"] = qty
                want = self._want_qty(p, cfg, r)
                if want != r["qty"]:
                    err = self._modify(r, qty=want) if r["state"] == "pending" else None
                    if err:
                        return {"ok": False, "msg": f"Kotak refused the size change: {err}"}
                    r["qty"] = want
                msg.append(f"size {qty}")
            if trigger not in (None, "", 0):
                t = float(trigger)
                t = _rt(t, c.tick, r["side"] == "B")
                if t <= 0 or self._crossed(r["side"], self.ltp_of(token), t):
                    return {"ok": False, "msg": f"stop {t:g} is already crossed by the price - not changed"}
                lim = self._limit(c, r["side"], t)
                if r["state"] == "pending" and r.get("oid"):
                    err = self._modify(r, trigger=t, limit=lim)
                    if err:
                        return {"ok": False, "msg": f"Kotak refused the new stop: {err}"}
                r.update(trigger=t, init=t, limit=lim, best=None)
                if r["state"] == "past":
                    self._send(r, c)
                audit.write("sl", what=f"SL of {r['symbol']} set by hand to {t:g}", kotak_id=r.get("oid"))
                msg.append(f"stop {t:g}")
            self._save()
            st = r["state"]
        self.on_change()
        where = "at Kotak" if st == "pending" else st
        return {"ok": st in ("pending", "watching"), "msg": f"{c.symbol}: stop-loss {r['trigger']:g} {where}"
                + (f" ({', '.join(msg)})" if msg else "")}

    def drop(self, token: int) -> dict:
        """Owner removes the stop-loss of one position (cancel at Kotak, no re-arm until it is flat)."""
        with self.lock:
            r = self.recs.get(token)
            if r is None:
                return {"ok": False, "msg": "no stop-loss on that position"}
            if self.live and r.get("oid"):
                err = self._cancel(r["oid"])
                if err:
                    return {"ok": False, "msg": f"Kotak refused to cancel the stop-loss: {err}"}
            self.recs.pop(token, None)
            self.done.add(token)
            self._save()
        audit.write("sl", what=f"stop-loss removed by owner: {r.get('symbol')}")
        self.on_change()
        return {"ok": True, "msg": f"stop-loss removed from {r.get('symbol')} (not re-armed until it is closed)"}

    def apply(self) -> dict:
        """APPLY button: every open option position matching AUTO ARM ON gets a stop at the CURRENT
        settings now - an existing stop is moved (modified in place at Kotak, no unprotected gap),
        a removed one (x) is placed again. Works whether Auto arm is ticked or not."""
        cfg = self.cfg()
        if self.live and not self.book.positions_fresh():
            return {"ok": False, "msg": "Kotak positions not read yet - try again in a moment"}
        with self.book.lock:
            pos = {t: dict(p) for t, p in self.book.pos.items() if p.get("net")}
        out, bad = [], []
        with self.lock:
            for t, p in pos.items():
                c = self.u.by_token.get(t)
                if c is None or c.kind not in ("CE", "PE"):
                    continue
                if (cfg["arm_on"] == "sell" and p["net"] > 0) or (cfg["arm_on"] == "buy" and p["net"] < 0):
                    continue
                self.done.discard(t)
                self.memo.pop(t, None)
                self.pause.pop(t, None)
                old = self.recs.get(t)
                if old and old.get("state") == "pending" and old.get("oid") and self.live:
                    # move the order already at Kotak to the new level
                    entry = old["entry"]
                    trig = self._levels(c, old["side"], entry, cfg)
                    lim = self._limit(c, old["side"], trig)
                    if self._crossed(old["side"], self.ltp_of(t), trig):
                        bad.append(f"{c.symbol}: price already beyond {trig:g} - stop left at {old['trigger']:g}")
                        continue
                    qty = self._want_qty(p, cfg)
                    err = self._modify(old, trigger=trig, limit=lim, qty=qty)
                    if err:
                        bad.append(f"{c.symbol}: Kotak refused the change ({err})")
                        continue
                    old.update(trigger=trig, init=trig, limit=lim, qty=qty, unit=cfg["sl_unit"], best=None,
                               trail=cfg["trail_auto"])
                    audit.write("sl", what=f"APPLY: SL {old['oid']} {c.symbol} moved to trigger {trig:g} limit {lim:g}")
                    out.append(f"{c.symbol} {trig:g}")
                    continue
                if old:
                    if self.live and old.get("oid") and old.get("state") != "hit":
                        self._cancel(old["oid"])
                    self.recs.pop(t, None)
                self._arm(t, p, c, cfg)
                r = self.recs.get(t) or {}
                if r.get("state") in ("pending", "watching"):
                    out.append(f"{c.symbol} {r['trigger']:g}")
                else:
                    bad.append(f"{c.symbol}: {r.get('msg') or r.get('state')}")
            self._save()
        self.on_change()
        if not out and not bad:
            return {"ok": True, "msg": f"no open {'' if cfg['arm_on'] == 'all' else cfg['arm_on'] + ' '}option position to protect"}
        unit = "%" if cfg["sl_unit"] == "%" else " pts"
        msg = (f"stop-loss at {cfg['sl_val']:g}{unit}: " + ", ".join(out)) if out else ""
        if bad:
            msg += (" · " if msg else "") + "NOT placed: " + "; ".join(bad)
        return {"ok": not bad, "msg": msg}

    # ── broker calls (live) ────────────────────────────────────────────────
    def _call(self, fn, **kw):
        return self.book._call(fn, **kw)

    def _err(self, r):
        from live_trading_options.mcx_terminal.live import resp_error
        return resp_error(r)

    def _cancel(self, oid):
        try:
            return self._err(self._call("cancel_order", order_id=str(oid)))
        except Exception as e:
            return str(e)

    def _status_of(self, oid):
        try:
            for row in self.book._rows():
                if str(row.get("nOrdNo")) == str(oid):
                    return str(row.get("ordSt", "")).lower()
        except Exception:
            return None
        return None

    def _tag(self):
        self._seq = (self._seq + 1) % 1000
        return f"{TAG_SL}{dt.datetime.now(IST):%H%M%S}{self._seq:03d}"

    def _place(self, r):
        from live_trading_options.mcx_terminal.live import extract_oid
        c = self.u.by_token[r["token"]]
        tag = self._tag()
        try:
            resp = self._call("place_order", exchange_segment=SEG, product=r["product"], price=self.book._px(r["limit"]),
                              order_type="SL", quantity=str(int(r["qty"])), validity="DAY", trading_symbol=c.symbol,
                              transaction_type=r["side"], trigger_price=self.book._px(r["trigger"]), tag=tag)
        except Exception as e:
            return None, str(e)
        err, oid = self._err(resp), extract_oid(resp)
        return (str(oid), None) if oid and not err else (None, err or "no order id")

    def _modify(self, r, trigger=None, limit=None, qty=None):
        c = self.u.by_token[r["token"]]
        try:
            resp = self._call("modify_order", order_id=str(r["oid"]), price=self.book._px(limit or r["limit"]),
                              order_type="SL", quantity=str(int(qty or r["qty"])), validity="DAY",
                              instrument_token=str(c.token), exchange_segment=SEG, product=r["product"],
                              trading_symbol=c.symbol, transaction_type=r["side"],
                              trigger_price=self.book._px(trigger or r["trigger"]), filled_quantity="0")
            return self._err(resp)
        except Exception as e:
            return str(e)

    # ── levels ─────────────────────────────────────────────────────────────
    def _levels(self, c, side, entry, cfg):
        """side = the CLOSING side (B closes a short)."""
        v = cfg["sl_val"]
        if side == "B":
            trig = entry * (1 + v / 100) if cfg["sl_unit"] == "%" else entry + v
            return _rt(trig, c.tick, True)
        trig = entry * (1 - v / 100) if cfg["sl_unit"] == "%" else entry - v
        return max(_rt(trig, c.tick, False), c.tick)

    def _limit(self, c, side, trig):
        if side == "B":
            return max(_rt(trig * (1 + LIMIT_PCT / 100), c.tick, True), trig + 2 * c.tick)
        return max(min(_rt(trig * (1 - LIMIT_PCT / 100), c.tick, False), trig - 2 * c.tick), c.tick)

    def _step(self, r, cfg):
        e = cfg["trail_every"]
        return r["entry"] * e / 100 if r["unit"] == "%" else e

    def _move(self, r, cfg):
        """How far the stop moves per trail step ("move SL"); 0 / empty = the same as `every`."""
        mv = cfg.get("trail_move") or cfg["trail_every"]
        return r["entry"] * mv / 100 if r["unit"] == "%" else mv

    @staticmethod
    def _crossed(side, ltp, trig):
        return ltp is not None and ((side == "B" and ltp >= trig) or (side == "S" and ltp <= trig))

    # ── the loop ───────────────────────────────────────────────────────────
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="AutoSL-" + ("live" if self.live else "paper")).start()
        return self

    def _run(self):
        while True:
            try:
                self.step()
            except Exception:
                log.exception("auto SL loop")
            self._kick.wait(LOOP_S)
            self._kick.clear()

    def step(self):
        cfg = self.cfg()
        if self.live and not self.book.positions_fresh():
            return                                    # never act on stale Kotak positions
        changed = False
        now = time.time()
        with self.book.lock:
            pos = {t: dict(p) for t, p in self.book.pos.items() if p.get("net")}
        with self.lock:
            # 1. live: Kotak's view of our SL orders first (a fill there is a HIT, not a vanished position)
            if self.live and (any(r.get("oid") for r in self.recs.values()) or now - self._rows_at > 15):
                if now - self._rows_at >= STATUS_EVERY_S:
                    changed |= self._broker_status(pos)
                    self._rows_at = now
            # 2. position gone or flipped -> its stop-loss goes too
            for t in list(self.recs):
                r = self.recs[t]
                p = pos.get(t)
                closing = "B" if p and p["net"] < 0 else "S" if p else None
                if closing != r["side"]:
                    if r.get("state") == "hit":
                        self.recs.pop(t)
                        changed = True
                        continue
                    if self.live and r.get("oid"):
                        err = self._cancel(r["oid"])
                        audit.write("sl", what=f"position {r['symbol']} closed/flipped -> SL {r['oid']} cancelled",
                                    result=err or "sent")
                    self.recs.pop(t)
                    changed = True
            for t in list(self.done) + list(self.memo):
                if t not in pos:
                    self.done.discard(t)
                    self.memo.pop(t, None)
                    changed = True
            # 3. arm new stop-losses
            if cfg["sl_auto"] and now >= self.pause_all:
                for t, p in pos.items():
                    c = self.u.by_token.get(t)
                    if t in self.recs or t in self.done or c is None or c.kind not in ("CE", "PE"):
                        continue
                    if now < self.pause.get(t, 0):
                        continue
                    if (cfg["arm_on"] == "sell" and p["net"] > 0) or (cfg["arm_on"] == "buy" and p["net"] < 0):
                        continue
                    changed |= self._arm(t, p, c, cfg)
            # 4. keep each stop in line: size, trail, retries
            for t, r in list(self.recs.items()):
                p, c = pos.get(t), self.u.by_token.get(t)
                if p is None or c is None:
                    continue
                changed |= self._maintain(t, r, p, c, cfg, now)
            if changed:
                self._save()
            exits, self._exits = self._exits, []
        # watched stops: exit OUTSIDE the lock (the exit itself calls before_reduce from another thread)
        for t, frac, sym, ltp in exits:
            res = self.book.exit_position(t, frac, source="stop-loss")
            self.notify({"ok": res.get("ok", False), "msg": f"STOP-LOSS HIT {sym} @ {ltp:g}: {res.get('msg', '')}"})
        if changed:
            self.on_change()

    def _want_qty(self, p, cfg, r=None):
        n = abs(p["net"])
        if ((r or {}).get("qty_mode") or cfg["qty"]) == "half":
            lots = max(1, (n // p["lot"]) // 2)
            return min(lots * p["lot"], n)
        return n

    def _product(self, p):
        bp = p.get("by_prod") or {}
        best = [pp for pp, n in sorted(bp.items(), key=lambda kv: -abs(kv[1])) if n and (n > 0) == (p["net"] > 0)]
        return best[0] if best and best[0] in ("NRML", "MIS") else "NRML"

    def _arm(self, t, p, c, cfg) -> bool:
        side = "B" if p["net"] < 0 else "S"
        entry = None
        if self.live and hasattr(self.book, "open_entry"):
            try:
                entry = self.book.open_entry(t)            # price of the lots still open, not the day's average
            except Exception:
                log.exception("open entry for %s", c.symbol)
        entry = float(entry or p["avg"] or 0)
        if entry <= 0:
            return False
        m = self.memo.get(t)
        trig = self._levels(c, side, entry, cfg)
        init, best, trail = trig, None, cfg["trail_auto"]
        if m and m.get("dir") == side and m.get("trigger"):
            trig, init, best, trail = m["trigger"], m.get("init") or trig, m.get("best"), m.get("trail", trail)
        r = {"token": t, "symbol": c.symbol, "side": side, "entry": entry, "unit": cfg["sl_unit"],
             "qty": self._want_qty(p, cfg), "trigger": trig, "init": init, "limit": self._limit(c, side, trig),
             "best": best, "trail": trail, "product": self._product(p), "oid": None, "state": "new", "msg": "",
             "at_broker": self.live, "t": time.time(), "t_mod": 0.0}
        self.recs[t] = r
        self._send(r, c)
        return True

    def _send(self, r, c):
        """Place (live) or start watching (paper / Kotak refused). A stop already crossed is NOT placed."""
        ltp = self.ltp_of(r["token"])
        if self._crossed(r["side"], ltp, r["trigger"]):
            r.update(state="past", msg=f"price {ltp:g} already beyond the stop {r['trigger']:g} - not placed")
            return
        if not self.live or r.get("watch"):
            r.update(state="watching", msg="" if not self.live else r.get("msg", ""))
            return
        oid, err = self._place(r)
        verb = "BUY" if r["side"] == "B" else "SELL"
        if oid:
            r.update(oid=oid, state="pending", msg="")
            log.warning("SL placed: %s %s %d trigger %s limit %s -> %s", verb, r["symbol"], r["qty"], r["trigger"], r["limit"], oid)
            audit.write("sl", what=f"SL order at Kotak: {verb} {r['symbol']} qty {r['qty']} trigger {r['trigger']:g} "
                                   f"limit {r['limit']:g}", kotak_id=oid)
        else:
            r.update(watch=True, state="watching", msg=f"Kotak refused the SL order ({err}) - terminal is watching instead")
            log.warning("SL order refused for %s: %s - watching", r["symbol"], err)
            audit.write("sl", what=f"SL order REFUSED for {r['symbol']}: {err} - terminal watches the price")
            self.notify({"ok": False, "msg": f"Kotak refused the stop-loss order for {r['symbol']}: {err}. "
                                             f"The terminal is watching the price instead."})

    def _maintain(self, t, r, p, c, cfg, now) -> bool:
        changed = False
        ltp = self.ltp_of(t)
        if r["state"] == "past":
            if not self._crossed(r["side"], ltp, r["trigger"]):
                self._send(r, c)
                return True
            return False
        if r["state"] == "hit":
            return False
        # size follows the position (added lots / partial fills)
        want = self._want_qty(p, cfg, r)
        if want != r["qty"]:
            if r["state"] == "pending":
                err = self._modify(r, qty=want)
                if not err:
                    r["qty"] = want
                    changed = True
            else:
                r["qty"] = want
                changed = True
        # trail: best price seen since the stop was set
        if ltp is not None:
            if r["best"] is None or (r["side"] == "B" and ltp < r["best"]) or (r["side"] == "S" and ltp > r["best"]):
                r["best"] = ltp                            # (not a screen change: saved with the next real one)
            if (r.get("trail") or cfg["trail_on"]) and r["best"] is not None:
                step = self._step(r, cfg)
                move = self._move(r, cfg)
                fav = (r["entry"] - r["best"]) if r["side"] == "B" else (r["best"] - r["entry"])
                k = int(fav // step) if step > 0 and fav > 0 else 0
                if k > 0:
                    nt = (_rt(r["init"] - k * move, c.tick, True) if r["side"] == "B"
                          else _rt(r["init"] + k * move, c.tick, False))
                    better = nt < r["trigger"] - 1e-9 if r["side"] == "B" else nt > r["trigger"] + 1e-9
                    if better and now - r.get("t_mod", 0) >= MODIFY_EVERY_S:
                        nl = self._limit(c, r["side"], nt)
                        err = self._modify(r, trigger=nt, limit=nl) if r["state"] == "pending" else None
                        r["t_mod"] = now
                        if err:
                            log.warning("SL trail modify %s refused: %s", r.get("oid"), err)
                        else:
                            audit.write("sl", what=f"SL trailed {r['symbol']}: {r['trigger']:g} -> {nt:g}")
                            r.update(trigger=nt, limit=nl)
                            changed = True
        # watching (paper, or Kotak refused): the terminal exits on the cross
        if r["state"] == "watching" and self._crossed(r["side"], ltp, r["trigger"]):
            frac = 1.0 if cfg["qty"] == "full" else 0.5
            self._hit(t, r, f"price {ltp:g} crossed {r['trigger']:g}")
            self._exits.append((t, frac, r["symbol"], ltp))
            return True
        return changed

    def _hit(self, t, r, why):
        r.update(state="hit", msg=why)
        self.done.add(t)
        self.memo.pop(t, None)
        log.warning("STOP-LOSS HIT %s (%s)", r["symbol"], why)
        audit.write("sl", what=f"STOP-LOSS HIT {r['symbol']} trigger {r['trigger']:g}", reason=why)

    def _broker_status(self, pos) -> bool:
        try:
            rows = self.book._rows()
        except Exception:
            return False
        changed = False
        by_oid = {str(x.get("nOrdNo")): x for x in rows}
        mine = {r.get("oid"): t for t, r in self.recs.items() if r.get("oid")}
        for oid, x in by_oid.items():
            tag = str(x.get("tag") or x.get("ig") or "")
            st = str(x.get("ordSt", "")).lower()
            working = not any(w in st for w in BROKER_FILLED + BROKER_DEAD)
            if tag.startswith(TAG_SL) and oid not in mine and working:
                err = self._cancel(oid)             # an SL order with nothing of ours behind it: never leave it
                log.warning("stray SL order %s (%s %s) cancelled: %s", oid, x.get("trdSym"), tag, err or "sent")
                audit.write("sl", what=f"stray SL order {oid} {x.get('trdSym')} cancelled", result=err or "sent")
        for oid, t in mine.items():
            r = self.recs[t]
            x = by_oid.get(oid)
            if x is None:
                continue
            st = str(x.get("ordSt", "")).lower()
            if r.get("bst") != st:
                r["bst"] = st
                changed = True
            if any(w in st for w in BROKER_FILLED):
                if r["state"] != "hit":
                    self._hit(t, r, f"filled at Kotak @ {x.get('avgPrc')}")
                    verb = "BOUGHT back" if r["side"] == "B" else "SOLD"
                    self.notify({"ok": True, "msg": f"STOP-LOSS HIT: {verb} {r['qty'] // max(1, self.u.by_token[t].lot)} "
                                                    f"lot(s) {r['symbol']} @ {x.get('avgPrc')}"})
                    changed = True
            elif "reject" in st:
                c = self.u.by_token[t]
                r.update(oid=None, watch=True, msg=f"Kotak rejected the SL order ({x.get('rejRsn') or st}) - terminal is watching")
                self._send(r, c)
                self.notify({"ok": False, "msg": f"Kotak rejected the stop-loss for {r['symbol']}: {x.get('rejRsn') or st}. "
                                                 f"The terminal is watching the price instead."})
                changed = True
            elif "cancel" in st:
                # cancelled outside the terminal (Kotak app): respect it, no re-arm until the position is closed
                self.recs.pop(t, None)
                self.done.add(t)
                audit.write("sl", what=f"SL order {oid} {r['symbol']} cancelled at Kotak - not re-armed")
                changed = True
            elif st in ("open", "trigger pending") or "open" in st:
                if st == "open":                       # triggered, limit not filled yet
                    r.setdefault("t_open", time.time())
                    if time.time() - r["t_open"] >= CHASE_S and not r.get("chased"):
                        q = self.book.quotes(t) if hasattr(self.book, "quotes") else {}
                        ref = (q or {}).get("ask" if r["side"] == "B" else "bid") or self.ltp_of(t)
                        if ref:
                            c = self.u.by_token[t]
                            nl = _rt(ref * (1.03 if r["side"] == "B" else 0.97), c.tick, r["side"] == "B")
                            err = self._modify(r, limit=nl)
                            r["chased"] = True
                            audit.write("sl", what=f"SL {oid} triggered but not filled -> limit {nl:g}", result=err or "sent")
        return changed
