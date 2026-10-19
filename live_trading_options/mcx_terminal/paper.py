"""
mcx_terminal/paper.py — the PAPER order book (no broker calls exist in this file).
=================================================================================

Kotak does not take MARKET orders through the API, so every order is a LIMIT:
  - "MKT"  : protective limit = best ask (buy) / best bid (sell) +/- buffer %, sweeps the book
  - "LMT"  : the price the trader typed; whatever does not fill rests until the book reaches it

Fills walk the REAL Kotak book (5-level depth when subscribed, else the touch line):
  buy  -> take asks from the best up to the limit, each level's quantity at most
  sell -> hit bids from the best down to the limit
A MKT order bigger than the visible depth fills the rest AT ITS LIMIT (worst case the order allows)
and says so in its reason; an LMT order's rest stays open and fills as later ticks reach it.

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
    def __init__(self, universe, quotes, on_change=None, path=None):
        """quotes(token) -> dict(ltp, bid, ask, bq, aq, depth=([(p,q)...] bids, [(p,q)...] asks))"""
        self.u = universe
        self.quotes = quotes
        self.on_change = on_change or (lambda: None)
        self.path = path or (C.DATA / f"paper_book_{today()}.json")
        self.orders: list[dict] = []
        self.pos: dict[int, dict] = {}
        self.lock = threading.RLock()
        self._load()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self):
        if not self.path.exists():
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
        q = self.quotes(token) or {}
        buf = C.MKT_BUFFER_PCT if buffer_pct is None else buffer_pct
        if kind == "MKT":
            ref = q.get("ask") if side == "B" else q.get("bid")
            ref = ref or q.get("ltp")
            if not ref:
                return {"ok": False, "msg": "no price for this contract yet"}
            limit = _round_tick(ref * (1 + buf / 100) if side == "B" else ref * (1 - buf / 100), c.tick, side == "B")
            limit = max(limit, c.tick)
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
                o = {"id": next(_ids), "ts": ts, "token": token, "symbol": c.symbol, "name": c.name, "side": side,
                     "qty": sq, "lot": c.lot, "kind": kind, "limit": limit, "filled": 0, "avg": 0.0,
                     "status": "OPEN", "reason": "", "source": source,
                     "slice": f"{i + 1}/{len(slices)}" if len(slices) > 1 else ""}
                fills = self._sweep(levels, side, limit, sq)
                for px, fq in fills:
                    self._fill(o, px, fq)
                rem = sq - o["filled"]
                notes = []
                if fills:
                    notes.append("filled from the book " + ", ".join(f"{fq}@{px:g}" for px, fq in fills))
                if rem and kind == "MKT":
                    self._fill(o, limit, rem)
                    notes.append(f"{rem} beyond visible depth filled at the limit {limit:g} (worst case)")
                elif rem:
                    notes.append(f"{rem} resting at {limit:g}")
                o["reason"] = "; ".join(notes) or "resting"
                o["status"] = "COMPLETE" if o["filled"] >= sq else "OPEN"
                self.orders.append(o)
                ids.append(o["id"])
            self._save()
        log.info("paper %s %s %d (%d lots) %s limit %s -> ids %s", side, c.symbol, qty, lots, kind, limit, ids)
        self.on_change()
        return {"ok": True, "ids": ids, "msg": f"{'BUY' if side == 'B' else 'SELL'} {lots} lot(s) {c.symbol}"}

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
        return {"ok": not bad, "msg": f"exit sent for {len(res)} position(s), {n_cancel} order(s) cancelled"
                + (f"; problems: {bad}" if bad else "")}

    # ── views ───────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self.lock:
            return {"positions": [dict(p) for p in self.pos.values() if p["net"] or p["realized"]],
                    "orders": [dict(o) for o in self.orders[-400:]][::-1]}

    def tokens(self) -> set:
        with self.lock:
            return {t for t, p in self.pos.items() if p["net"]} | {o["token"] for o in self.orders if o["status"] == "OPEN"}
