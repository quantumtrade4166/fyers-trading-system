"""
mcx_terminal/paper.py — the PAPER order book (no broker calls exist in this file).
=================================================================================

Kotak does not take MARKET orders through the API, so every order is a LIMIT:
  - "MKT"  : system-priced limit = reference (best ask/bid or LTP, per settings) +/- buffer (% or points)
  - "LMT"  : the price the trader typed; whatever does not fill rests until the book reaches it
An order that REDUCES a position uses the exit settings, anything else the entry settings.
housekeep() (every 0.5 s) re-prices open system orders to the current reference each second and,
if set, pushes any order still open after N seconds through the book (best price +/- 1 %).

Fills walk the REAL Kotak book (5-level depth when subscribed, else the touch line):
  buy  -> take asks from the best up to the limit, each level's quantity at most
  sell -> hit bids from the best down to the limit
If a MKT order uses up every visible level inside its limit, the rest fills AT ITS LIMIT (worst case
the order allows; deeper levels are not visible) and says so; if the book beyond its limit is visible
the rest WAITS (open), exactly like a real limit order. LMT rests always wait.

Orders bigger than the exchange freeze quantity are split into slices, all placed together; the
slices share one working copy of the book so they cannot take the same quantity twice.

Positions use the average-cost method (rupees = points x units x multiplier; GOLDM multiplier 0.1).
Everything is saved to data/paper_book_YYYY-MM-DD.json after each change (survives restarts).
"""
import datetime as dt
import itertools
import json
import logging
import math
import threading
import time

from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.paper")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
_ids = itertools.count(1)


def today() -> str:
    return dt.datetime.now(IST).date().isoformat()


def _round_tick(px: float, tick: float, up: bool) -> float:
    if not tick:
        return round(px, 2)
    n = px / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 2)


class PaperBook:
    def __init__(self, universe, quotes, on_change=None, path=None, settings=None):
        """quotes(token) -> dict(ltp, bid, ask, bq, aq, depth=([(p,q)...] bids, [(p,q)...] asks))"""
        self.u = universe
        self.quotes = quotes
        self.on_change = on_change or (lambda: None)
        self.settings = settings or (lambda: {"entry_ref": "touch", "entry_buffer": C.MKT_BUFFER_PCT, "entry_unit": "%",
                                              "exit_ref": "touch", "exit_buffer": C.MKT_BUFFER_PCT, "exit_unit": "%",
                                              "reprice": False, "market_after_s": 0, "product": "NRML", "session": "AUTO"})
        self.path = path or (C.DATA / f"paper_book_{today()}.json")
        self.orders: list[dict] = []
        self.pos: dict[int, dict] = {}
        self.lock = threading.RLock()
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self):
        if not self.path.exists():
            self._carry_forward()
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.orders = d.get("orders", [])
            self.pos = {int(k): v for k, v in d.get("positions", {}).items()}
            global _ids
            _ids = itertools.count(max([o["id"] for o in self.orders] or [0]) + 1)
            log.info("paper book restored: %d orders, %d positions", len(self.orders), len(self.pos))
        except Exception:
            log.exception("paper book load failed; starting empty")

    def _carry_forward(self):
        """A new day: bring over yesterday's OPEN positions (NRML carry) at their average price.
        Realized P&L starts at 0 for the new day. Contracts no longer listed (expired) are dropped."""
        prev = sorted(p for p in self.path.parent.glob("paper_book_*.json") if p.name < self.path.name)
        if not prev:
            return
        try:
            d = json.loads(prev[-1].read_text(encoding="utf-8"))
        except Exception:
            log.exception("carry-forward: cannot read %s", prev[-1].name)
            return
        kept, dropped = 0, []
        for k, p in d.get("positions", {}).items():
            if not p.get("net"):
                continue
            tok = int(k)
            if tok not in self.u.by_token:
                dropped.append(p.get("symbol", k))
                continue
            self.pos[tok] = {**p, "realized": 0.0, "bought": 0, "sold": 0, "carried": True}
            kept += 1
        if kept or dropped:
            log.warning("carry-forward from %s: %d open position(s) kept, expired/unlisted dropped: %s",
                        prev[-1].name, kept, dropped)
            self._save()

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"orders": self.orders, "positions": self.pos}, default=str), encoding="utf-8")
        tmp.replace(self.path)

    # ── book walking ────────────────────────────────────────────────────────
    def _levels(self, token, side):
        """Opposite-side levels to trade against: [(price, qty)], best first."""
        q = self.quotes(token) or {}
        dp = q.get("depth")
        if dp:
            lv = dp[1] if side == "B" else dp[0]
            lv = [(p, qq) for p, qq in lv if p and qq]
            if lv:
                return lv
        p, qq = (q.get("ask"), q.get("aq")) if side == "B" else (q.get("bid"), q.get("bq"))
        return [(p, qq)] if p and qq else []

    @staticmethod
    def _sweep(levels, side, limit, qty):
        """Take up to qty from levels within limit. Mutates levels (working copy). -> [(px, q)]"""
        fills = []
        for i, (p, avail) in enumerate(levels):
            if qty <= 0:
                break
            if (side == "B" and p > limit + 1e-9) or (side == "S" and p < limit - 1e-9):
                break
            take = min(qty, avail)
            if take > 0:
                fills.append((p, take))
                qty -= take
                levels[i] = (p, avail - take)
        return fills

    # ── pricing ─────────────────────────────────────────────────────────────
    def _role(self, token, side) -> str:
        p = self.pos.get(token)
        if p and p["net"] and ((p["net"] > 0) == (side == "S")):
            return "exit"
        return "entry"

    def _price(self, token, side, role, st):
        """System limit price for an "at the market" order (None = no quote)."""
        c = self.u.by_token[token]
        q = self.quotes(token) or {}
        if st[f"{role}_ref"] == "ltp":
            ref = q.get("ltp")
        else:
            ref = q.get("ask") if side == "B" else q.get("bid")
        ref = ref or q.get("ltp")
        if not ref:
            return None
        buf = float(st[f"{role}_buffer"] or 0)
        adj = ref * buf / 100 if st[f"{role}_unit"] == "%" else buf
        px = ref + adj if side == "B" else ref - adj
        return max(_round_tick(px, c.tick, side == "B"), c.tick)

    def _aggressive(self, token, side):
        c = self.u.by_token[token]
        q = self.quotes(token) or {}
        ref = (q.get("ask") if side == "B" else q.get("bid")) or q.get("ltp")
        if not ref:
            return None
        return max(_round_tick(ref * (1.01 if side == "B" else 0.99), c.tick, side == "B"), c.tick)

    @staticmethod
    def _book_beyond(levels, side, limit) -> bool:
        """Is a visible level with quantity left priced beyond our limit (the market is past us)?"""
        return any(qq > 0 and ((side == "B" and p > limit + 1e-9) or (side == "S" and p < limit - 1e-9))
                   for p, qq in levels)

    # ── orders ──────────────────────────────────────────────────────────────
    def place(self, token: int, side: str, qty: int, kind: str = "MKT", limit: float | None = None,
              source: str = "", buffer_pct: float | None = None) -> dict:
        """-> {"ok": bool, "ids": [...], "msg": str}"""
        c = self.u.by_token.get(token)
        if c is None:
            return {"ok": False, "msg": "unknown contract"}
        side = side.upper()[:1]
        if side not in ("B", "S"):
            return {"ok": False, "msg": "side must be B or S"}
        if qty <= 0 or qty % c.lot:
            return {"ok": False, "msg": f"qty must be a positive multiple of the lot ({c.lot})"}
        lots = qty // c.lot
        if lots > C.MAX_LOTS_PER_ORDER:
            return {"ok": False, "msg": f"{lots} lots is over the cap of {C.MAX_LOTS_PER_ORDER} lots per order"}
        st = self.settings()
        if buffer_pct is not None:                                   # explicit override (tests / callers)
            st = {**st, "entry_buffer": buffer_pct, "exit_buffer": buffer_pct, "entry_unit": "%", "exit_unit": "%"}
        with self.lock:
            role = self._role(token, side)
        if kind == "MKT":
            limit = self._price(token, side, role, st)
            if not limit:
                return {"ok": False, "msg": "no price for this contract yet"}
        else:
            if not limit or limit <= 0:
                return {"ok": False, "msg": "limit price required"}
            limit = _round_tick(limit, c.tick, side == "S")      # never round a limit into a worse price
        slices = [c.freeze] * (qty // c.freeze) + ([qty % c.freeze] if qty % c.freeze else []) if c.freeze else [qty]
        with self.lock:
            levels = list(self._levels(token, side))                 # one working copy for all slices
            ids = []
            ts = dt.datetime.now(IST).strftime("%H:%M:%S.%f")[:-3]
            for i, sq in enumerate(slices):
                o = {"id": next(_ids), "ts": ts, "t0": time.time(), "token": token, "symbol": c.symbol,
                     "name": c.name, "side": side, "qty": sq, "lot": c.lot, "kind": kind, "limit": limit,
                     "filled": 0, "avg": 0.0, "status": "OPEN", "reason": "", "source": source, "role": role,
                     "product": st.get("product", "NRML"), "session": st.get("session", "AUTO"), "t_rep": time.time(),
                     "slice": f"{i + 1}/{len(slices)}" if len(slices) > 1 else ""}
                fills = self._sweep(levels, side, limit, sq)
                for px, fq in fills:
                    self._fill(o, px, fq)
                rem = sq - o["filled"]
                notes = []
                if fills:
                    notes.append("filled from the book " + ", ".join(f"{fq}@{px:g}" for px, fq in fills))
                if rem and kind == "MKT" and not self._book_beyond(levels, side, limit):
                    self._fill(o, limit, rem)
                    notes.append(f"{rem} beyond visible depth filled at the limit {limit:g} (worst case)")
                elif rem:
                    notes.append(f"{rem} waiting at {limit:g}")
                o["reason"] = "; ".join(notes) or "resting"
                o["status"] = "COMPLETE" if o["filled"] >= sq else "OPEN"
                self.orders.append(o)
                ids.append(o["id"])
            self._save()
        log.info("paper %s %s %d (%d lots) %s limit %s -> ids %s", side, c.symbol, qty, lots, kind, limit, ids)
        self.on_change()
        mine = [o for o in self.orders if o["id"] in ids]
        filled = sum(o["filled"] for o in mine)
        avg = sum(o["avg"] * o["filled"] for o in mine) / filled if filled else None
        word = "BOUGHT" if side == "B" else "SOLD"
        if filled >= qty:
            state, msg = "filled", f"{word} {lots} lot{'s' if lots != 1 else ''} {c.symbol} @ {avg:,.2f}"
        elif filled:
            state, msg = "partial", (f"{word} {filled // c.lot} of {lots} lots {c.symbol} @ {avg:,.2f} · "
                                     f"{(qty - filled) // c.lot} waiting at {limit:g}")
        else:
            state, msg = "waiting", f"{'BUY' if side == 'B' else 'SELL'} {lots} lot{'s' if lots != 1 else ''} {c.symbol} waiting at {limit:g}"
        return {"ok": True, "ids": ids, "state": state, "msg": msg}

    def _fill(self, o, px, q):
        o["avg"] = (o["avg"] * o["filled"] + px * q) / (o["filled"] + q)
        o["filled"] += q
        self._apply(o["token"], o["side"], px, q)

    def _apply(self, token, side, px, q):
        c = self.u.by_token[token]
        p = self.pos.setdefault(token, {"token": token, "symbol": c.symbol, "name": c.name, "lot": c.lot,
                                        "mult": c.mult, "net": 0, "avg": 0.0, "realized": 0.0,
                                        "bought": 0, "sold": 0})
        sq = q if side == "B" else -q
        net = p["net"]
        if side == "B":
            p["bought"] += q
        else:
            p["sold"] += q
        if net == 0 or (net > 0) == (sq > 0):                       # opening / adding
            p["avg"] = (p["avg"] * abs(net) + px * q) / (abs(net) + q)
            p["net"] = net + sq
        else:                                                        # reducing / flipping
            close = min(abs(net), q)
            p["realized"] += (px - p["avg"]) * close * (1 if net > 0 else -1) * c.mult
            p["net"] = net + sq
            if p["net"] == 0:
                p["avg"] = 0.0
            elif (p["net"] > 0) != (net > 0):                        # flipped through zero
                p["avg"] = px

    def on_tick(self, token):
        """Try resting LMT orders of this token against the new book."""
        changed = False
        with self.lock:
            open_o = [o for o in self.orders if o["token"] == token and o["status"] == "OPEN"]
            if not open_o:
                return
            for side in ("B", "S"):
                levels = list(self._levels(token, side))
                for o in (x for x in open_o if x["side"] == side):
                    fills = self._sweep(levels, side, o["limit"], o["qty"] - o["filled"])
                    for px, fq in fills:
                        self._fill(o, px, fq)
                        changed = True
                    if o["filled"] >= o["qty"]:
                        o["status"] = "COMPLETE"
                        o["reason"] += f"; rest filled {dt.datetime.now(IST):%H:%M:%S}"
            if changed:
                self._save()
        if changed:
            self.on_change()

    def housekeep(self):
        """Every ~0.5 s: re-price open system orders (each second) and push old orders through."""
        st = self.settings()
        now = time.time()
        changed = False
        with self.lock:
            for o in self.orders:
                if o["status"] != "OPEN":
                    continue
                rem = o["qty"] - o["filled"]
                levels = list(self._levels(o["token"], o["side"]))
                mkt_after = float(st.get("market_after_s") or 0)
                if mkt_after and now - o.get("t0", now) >= mkt_after:
                    lim = self._aggressive(o["token"], o["side"])
                    if lim:
                        o["limit"] = lim
                        for px, fq in self._sweep(levels, o["side"], lim, rem):
                            self._fill(o, px, fq)
                        left = o["qty"] - o["filled"]
                        if left:
                            self._fill(o, lim, left)
                        o["status"] = "COMPLETE"
                        o["reason"] += f"; pushed to market after {mkt_after:g}s (limit {lim:g})"
                        changed = True
                    continue
                if o["kind"] == "MKT" and st.get("reprice") and now - o.get("t_rep", 0) >= 1.0:
                    o["t_rep"] = now
                    lim = self._price(o["token"], o["side"], o.get("role", "entry"), st)
                    if lim and abs(lim - o["limit"]) > 1e-9:
                        o["limit"] = lim
                        o["reprices"] = o.get("reprices", 0) + 1
                        changed = True
                    for px, fq in self._sweep(levels, o["side"], o["limit"], rem):
                        self._fill(o, px, fq)
                        changed = True
                    if o["filled"] >= o["qty"]:
                        o["status"] = "COMPLETE"
                        o["reason"] += f"; filled after {o.get('reprices', 0)} re-price(s)"
            if changed:
                self._save()
        if changed:
            self.on_change()

    def cancel(self, oid: int) -> dict:
        with self.lock:
            for o in self.orders:
                if o["id"] == oid and o["status"] == "OPEN":
                    o["status"] = "CANCELLED" if not o["filled"] else "PART-CANCELLED"
                    self._save()
                    break
            else:
                return {"ok": False, "msg": "no open order with that id"}
        self.on_change()
        return {"ok": True, "msg": f"cancelled #{oid}"}

    def cancel_all(self) -> int:
        n = 0
        with self.lock:
            for o in self.orders:
                if o["status"] == "OPEN":
                    o["status"] = "CANCELLED" if not o["filled"] else "PART-CANCELLED"
                    n += 1
            if n:
                self._save()
        if n:
            self.on_change()
        return n

    def exit_position(self, token, fraction: float = 1.0, source="exit") -> dict:
        with self.lock:
            p = self.pos.get(token)
            if not p or not p["net"]:
                return {"ok": False, "msg": "no open position"}
            lots = int(abs(p["net"]) // p["lot"] * fraction + 1e-9) or 1
            qty = min(lots * p["lot"], abs(p["net"]))
            side = "S" if p["net"] > 0 else "B"
        return self.place(token, side, qty, "MKT", source=source)

    def exit_all(self, fraction: float = 1.0) -> dict:
        n_cancel = self.cancel_all()
        res = [self.exit_position(t, fraction, "exit all") for t, p in list(self.pos.items()) if p["net"]]
        bad = [r["msg"] for r in res if not r["ok"]]
        states = {r.get("state") for r in res if r["ok"]}
        state = "filled" if states <= {"filled"} else "waiting"
        return {"ok": not bad, "state": state if res else "filled",
                "msg": (f"EXITED {len(res)} position(s)" if state == "filled" else f"exit sent for {len(res)} position(s), some waiting")
                + (f", {n_cancel} order(s) cancelled" if n_cancel else "") + (f" · PROBLEM: {'; '.join(bad)}" if bad else "")}

    def basket(self, legs, source="basket") -> dict:
        """legs = [{token, side, qty}] placed together; BUYS FIRST (hedges before the sells, as live
        trading needs for margin), then sells."""
        legs = sorted(legs, key=lambda l: 0 if l["side"].upper().startswith("B") else 1)
        res = [self.place(int(l["token"]), l["side"], int(l["qty"]), "MKT", source=source) for l in legs]
        bad = [r["msg"] for r in res if not r["ok"]]
        ids = [i for r in res if r["ok"] for i in r["ids"]]
        states = {r.get("state") for r in res if r["ok"]}
        state = "filled" if states == {"filled"} else "waiting" if states else None
        lines = [r["msg"] for r in res if r["ok"]]
        return {"ok": not bad, "ids": ids, "state": state,
                "msg": " · ".join(lines) + (f" · PROBLEM: {'; '.join(bad)}" if bad else "")}

    def move(self, token, steps: int, away: bool) -> dict:
        """Roll an open leg `steps` strikes: away = further OTM (CE up, PE down), close = toward ATM.
        Closes the old strike and opens the same size at the new one, in one basket."""
        c = self.u.by_token.get(token)
        with self.lock:
            p = self.pos.get(token)
            if not c or c.kind not in ("CE", "PE") or not p or not p["net"]:
                return {"ok": False, "msg": "no open option position to move"}
            net = p["net"]
        chain = self.u.products[c.name].chains.get(c.expiry, {})
        strikes = sorted(chain)
        i = strikes.index(c.strike)
        d = steps if (c.kind == "CE") == away else -steps
        j = i + d
        if not 0 <= j < len(strikes):
            return {"ok": False, "msg": "no strike that far in this expiry"}
        new = chain[strikes[j]][c.kind]
        close_side, open_side = ("S", "B") if net > 0 else ("B", "S")
        r = self.basket([{"token": token, "side": close_side, "qty": abs(net)},
                         {"token": new.token, "side": open_side, "qty": abs(net)}], source="move")
        r["msg"] = f"moved {c.symbol} -> {new.symbol}: " + r["msg"]
        return r

    # ── views ───────────────────────────────────────────────────────────────
    def positions(self) -> list:
        with self.lock:
            return [dict(p) for p in self.pos.values()]

    def snapshot(self) -> dict:
        with self.lock:
            return {"positions": [dict(p) for p in self.pos.values() if p["net"] or p["realized"]],
                    "orders": [dict(o) for o in self.orders[-400:]][::-1]}

    def tokens(self) -> set:
        with self.lock:
            return {t for t, p in self.pos.items() if p["net"]} | {o["token"] for o in self.orders if o["status"] == "OPEN"}
