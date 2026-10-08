"""
vwap/leg_exec.py — sell or buy ONE leg, paper or live, through one interface.
=============================================================================

    ex.execute(chain, strike, typ, side, qty, kind, floor=None) -> result dict

The book never branches on paper-vs-live; this does.

PAPER  the contract's live L2 book walked at full size (chain.book_fill) — what the
       paper versions have always done.

LIVE   marketable LIMIT orders, immediate-or-cancel, priced off the live L2 book:
         the price that sweeps the remaining size, then a buffer through it
         (sell below, buy above), rounded to the contract's tick.
       ENTRY  (`floor` given) never sells below `floor` — the bad-fill guard. Up to
              `entry_attempts` tries; whatever did not fill is reported, and the book
              decides (it unwinds a half-filled strangle).
       EXIT   must never be skipped: `exit_buffers` widen on every attempt, then a
              MARKET order. If even that leaves contracts open the result says so and
              the book retries on the next poll — the leg stays OPEN in the book until
              Delta confirms it is bought back.
       A BUY is capped at the short Delta actually shows for the contract (own book,
       never broker net): a buy can only cover a real short, never open a long.

Every live result carries the order ids, filled size, average price and the
commission Delta charged.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import math
import time
import datetime as dt

from core.api import orderbook
from core.fills import walk_book


def _round_tick(price, tick, side):
    """Sells round DOWN, buys round UP — always through the price, never short of it."""
    tick = tick or 0.1
    n = price / tick
    n = math.floor(n + 1e-9) if side == "sell" else math.ceil(n - 1e-9)
    return round(max(tick, n * tick), 6)


def _sweep_price(levels, qty):
    """Worst price the visible book needs to fill `qty`; None if that side is empty."""
    w = walk_book(levels, qty)
    if w["price"] is None:
        return None, w
    return w["worst"], w


class PaperExec:
    live = False

    def __init__(self, contracts_per_leg):
        self.qty = contracts_per_leg

    def execute(self, chain, strike, typ, side, qty, kind, floor=None, tag=""):
        f = chain.book_fill(strike, typ, "SELL" if side == "sell" else "BUY", qty)
        if f.get("price") is None:
            return {"ok": False, "filled": 0, "price": None, "fee": 0.0,
                    "error": "no price", "orders": []}
        if floor is not None and side == "sell" and f["price"] < floor:
            return {"ok": False, "filled": 0, "price": f["price"], "fee": 0.0,
                    "error": "below floor", "orders": [], "retry": True}
        return {"ok": True, "filled": qty, "price": f["price"], "fee": None,
                "source": f.get("source"), "orders": []}


class LiveExec:
    live = True

    def __init__(self, client, cfg: dict, log=print):
        self.c = client
        self.entry_attempts = int(cfg.get("entry_attempts", 3))
        self.entry_buffer = float(cfg.get("entry_buffer_pct", 1.0)) / 100
        self.exit_buffers = [float(x) / 100 for x in cfg.get("exit_buffers_pct", [2, 5, 10, 25])]
        self.market_last = bool(cfg.get("exit_market_fallback", True))
        self.leverage = cfg.get("leverage")
        self.log = log
        self._seq = 0
        self._lev_done = set()

    def _cid(self, tag, side):
        self._seq = (self._seq + 1) % 1000
        return f"bvw{dt.datetime.now():%d%H%M%S}{side[0]}{tag[:4]}{self._seq:03d}"[:32]

    def ensure_leverage(self, pid, sym):
        """Set the configured order leverage on this contract once, before its first
        sell. Each daily option is a new product with Delta's default (100x seen
        2026-10-04), which needs about twice the margin of 200x. A failure is logged
        and the sell goes ahead — the worst case is an insufficient-margin refusal,
        which the book handles (unwind + stop for the day)."""
        if not self.leverage or pid in self._lev_done:
            return
        try:
            cur = self.c.get_leverage(pid)
            if cur is None or abs(cur - float(self.leverage)) > 1e-9:
                got = self.c.set_leverage(pid, self.leverage)
                self.log(f"  [live] leverage {sym}: {cur}x -> {got}x")
            self._lev_done.add(pid)
        except Exception as e:
            self.log(f"  [live] !! leverage {sym} not set ({e}) — selling anyway")

    def broker_short(self, product_id):
        """Contracts Delta shows SHORT on this product (positive number)."""
        size = self.c.positions().get(int(product_id), 0)
        return max(0, -size)

    def execute(self, chain, strike, typ, side, qty, kind, floor=None, tag=""):
        c = chain.contract(strike, typ)
        if c is None:
            return {"ok": False, "filled": 0, "price": None, "fee": 0.0,
                    "error": f"no contract {strike}{typ}", "orders": []}
        pid, sym, tick = c["product_id"], c["symbol"], c.get("tick_size") or 0.1

        if side == "buy":
            short = self.broker_short(pid)
            if short < qty:
                self.log(f"  !! {sym}: own book wants to buy {qty} but Delta shows short {short} "
                         f"— buying only {short} (never open a long)")
                qty = short
            if qty <= 0:
                return {"ok": True, "filled": 0, "price": None, "fee": 0.0, "orders": [],
                        "broker_flat": True}

        if side == "sell":
            self.ensure_leverage(pid, sym)

        t0 = time.monotonic()
        done, cost, fee, orders, err = 0, 0.0, 0.0, [], None
        attempts = (self.entry_attempts if side == "sell" else len(self.exit_buffers))
        for i in range(attempts + (1 if side == "buy" and self.market_last else 0)):
            left = qty - done
            if left <= 0:
                break
            use_market = side == "buy" and i >= attempts
            try:
                if use_market:
                    o = self.c.place_order(pid, side, left, order_type="market_order",
                                           reduce_only=True, client_order_id=self._cid(tag, side))
                    limit = None
                else:
                    book = orderbook(sym)
                    levels = book.get("buy") if side == "sell" else book.get("sell")
                    sweep, w = _sweep_price(levels, left)
                    if sweep is None:
                        err = "empty book side"
                        if side == "sell":
                            break
                        time.sleep(0.5)
                        continue
                    buf = self.entry_buffer if side == "sell" else self.exit_buffers[i]
                    raw = sweep * (1 - buf) if side == "sell" else sweep * (1 + buf) + tick
                    if side == "sell" and floor is not None and raw < floor:
                        raw = floor
                        if sweep < floor:
                            err = f"book {sweep} below floor {floor:.2f}"
                            break
                    limit = _round_tick(raw, tick, side)
                    o = self.c.place_order(pid, side, left, order_type="limit_order",
                                           limit_price=limit, tif="ioc",
                                           reduce_only=(side == "buy"),
                                           client_order_id=self._cid(tag, side))
                from live.delta_client import order_result
                r = order_result(o, left)
                orders.append({"id": r["id"], "cid": r["client_order_id"], "limit": limit,
                               "market": use_market, "filled": r["filled"], "price": r["price"],
                               "fee": r["fee"], "state": r["state"]})
                if r["filled"] and r["price"] is not None:
                    cost += r["filled"] * r["price"]
                    done += r["filled"]
                fee += r["fee"]
                self.log(f"  [live] {kind} {side} {sym} {left} @ {'MKT' if use_market else limit}"
                         f" -> filled {r['filled']} avg {r['price']} ({r['state']})")
            except Exception as e:
                code = getattr(e, "code", None)
                err = f"{code or type(e).__name__}: {e}"
                self.log(f"  [live] !! {kind} {side} {sym} attempt {i + 1}: {err}")
                if code == "insufficient_margin":
                    return {"ok": False, "filled": done, "price": cost / done if done else None,
                            "fee": fee, "error": err, "orders": orders, "margin": True,
                            "ms": int((time.monotonic() - t0) * 1000)}
                if side == "sell" and code not in ("network", "rate_limited"):
                    break
                time.sleep(0.5)
        price = round(cost / done, 6) if done else None
        return {"ok": done >= qty, "filled": done, "price": price, "fee": round(fee, 6),
                "error": err if done < qty else None, "orders": orders, "wanted": qty,
                "ms": int((time.monotonic() - t0) * 1000)}
