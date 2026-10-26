"""
strategy_builder/orders.py — deploy a builder strategy to brokers, keep its own book, exit it.
=============================================================================================

One DEPLOYMENT = one strategy on one broker account. Deploying "hedge 17 oct" to Zerodha and
Kotak P makes two deployments, each run by its own thread, so a slow or failing broker never
holds up the other.

SAFETY RULES (each one is a test in test_orders.py)
  * Nothing is placed unless the desk is ARMED (control.json, set from the page by the owner).
    Exits (square-off, SL, target, exit time) always work, armed or not.
  * Orders only 09:15-15:30 IST Mon-Fri.
  * Entry: BUY legs (hedges) are filled FIRST. If any buy leg does not fill completely, NO sell
    leg is sent — the account is never left with a naked short because a hedge failed.
  * Exit: shorts are bought back FIRST, then longs are sold — same reason, in reverse.
  * Own book only: every order carries this deployment's tag; positions are rebuilt from OUR
    fills, never from the broker's netted position (which mixes manual / strangle trades).
  * A lost order response is never retried blindly: Kite via place_limit_verified, Kotak by
    looking up the exact unique tag in the order book before deciding.
  * Size caps: MAX_LOTS per leg per broker; big orders are sliced below the exchange freeze.
  * A restart mid-placement does NOT carry on by itself: the deployment is marked
    'interrupted' and the page shows it, so a person decides (square off / leave it).

PAPER (broker id "paper"): same desk, same book, same SL/target/square-off, but orders fill against
the LIVE Fyers bid/ask (buy at the ask, sell at the bid; a limit fills when the market touches it).
Paper never needs arming — it cannot reach a broker.

QUICK orders (keyboard / Trade-mode clicks) go into one "Quick · <broker>" deployment per broker per day;
each order adds to that book (same contract = same leg). Hedge-first still applies within one click.

HISTORY: every 5 s each deployment with a position (and once more when it goes flat) appends
{t, mtm, ce, pe, imb} to data/deployments/history/<id>.jsonl -> the MTM and CE/PE-imbalance charts.
CE/PE imbalance = (short CE premium - short PE premium) / total short premium x 100, at live LTP
(same definition as the MCX terminal).

Marketable mode (default): each order is priced off the live order book (depth sweep + a few
ticks), re-priced up to 4 times if it does not fill — like a market order with a ceiling.
Limit mode: each leg rests at the price typed in the playground until filled or cancelled.
"""
import datetime as dt
import json
import logging
import secrets
import threading
import time
from pathlib import Path

from live_trading_options.strategy_builder import config as C

log = logging.getLogger("sb.orders")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

DEP_DIR = C.DATA / "deployments"
CONTROL = C.DATA / "control.json"

MAX_LOTS = 50                                   # per leg per broker, after the multiplier
MAX_LEGS = 10
SLICE_LOTS = {"NIFTY": 20, "SENSEX": 40}        # per order — comfortably under the freeze qty
ENTRY_CUSHIONS = (2, 5, 10, 20)                 # ticks through the book, per chase round
EXIT_CUSHIONS = (2, 6, 12, 25, 50)              # exits must fill — reach further
FILL_WAIT = 4.0                                 # seconds per round before re-pricing
FINAL = ("COMPLETE", "REJECTED", "CANCELLED")
HIST_EVERY = 5.0                                # seconds between MTM / imbalance samples


def now_ist():
    return dt.datetime.now(IST)


def market_open(t: dt.datetime | None = None) -> bool:
    t = t or now_ist()
    return t.weekday() < 5 and dt.time(9, 15) <= t.time() < dt.time(15, 30)


# ── broker adapters: the only code that differs between Kite and Kotak ─────────────────
class KiteAdapter:
    def __init__(self, kx):
        self.kx = kx

    def side(self, s):
        return "BUY" if s == "B" else "SELL"

    def price(self, c, k, side, qty, fallback, cushion):
        return self.kx.marketable_price(c, k["exchange"], k["symbol"], self.side(side), qty,
                                        fallback=fallback, cushion_ticks=cushion)

    def place(self, c, k, side, qty, price, product, tag):
        oid = self.kx.place_limit_verified(c, k["symbol"], k["exchange"], self.side(side), qty,
                                           price, product=product, tag=tag)
        return str(oid), tag

    def status(self, c, oid):
        s = self.kx.order_status(c, oid)
        st = (s.get("status") or "")
        msg = None
        if st == "REJECTED":
            try:
                msg = (c.order_history(oid) or [{}])[-1].get("status_message")
            except Exception:
                pass
        norm = st if st in FINAL else ("OPEN" if st else None)
        return {"status": norm, "filled": int(s.get("filled_qty") or 0),
                "avg": s.get("avg_price"), "time": s.get("fill_time"), "msg": msg}

    def cancel(self, c, oid):
        try:
            self.kx.cancel(c, oid)
        except Exception as e:
            log.info("kite cancel %s: %s", oid, e)


class KotakAdapter:
    def __init__(self, ke):
        self.ke = ke

    def price(self, c, k, side, qty, fallback, cushion):
        return self.ke.marketable_price(c, k["token"], k["exchange"], side, qty,
                                        fallback=fallback, cushion_ticks=cushion)

    def place(self, c, k, side, qty, price, product, tag):
        """Like kotak_executor.place_limit, but the unique tag is made HERE so a lost response
        can be checked against the order book before anything is sent again."""
        ke = self.ke
        otag = ke._unique_tag(tag)
        try:
            resp = c.place_order(exchange_segment=k["exchange"], product=product,
                                 price=str(ke._round_tick(price)), order_type=ke.ORDER_TYPE_LIMIT,
                                 quantity=str(int(qty)), validity=ke.VALIDITY_DAY,
                                 trading_symbol=k["symbol"], transaction_type=ke._txn_code(side),
                                 tag=otag)
        except Exception:
            time.sleep(1.5)
            for o in ke._order_rows(c):
                if ke._dig(o, "tag", "orderTag") == otag:
                    return str(ke._dig(o, *ke._OID_KEYS)), otag      # it DID land — adopt it
            raise
        err = ke._resp_error(resp)
        if err:
            raise RuntimeError(f"Kotak rejected: {err}")
        oid = ke._extract_oid(resp)
        if not oid:
            raise RuntimeError(f"Kotak: no order id in {str(resp)[:200]}")
        return str(oid), otag

    def status(self, c, oid):
        s = self.ke.order_status(c, oid)
        raw = str(s.get("status") or "").lower()
        if "reject" in raw:
            st = "REJECTED"
        elif "cancel" in raw:
            st = "CANCELLED"
        elif raw and "incomplete" not in raw and any(w in raw for w in ("complete", "traded", "filled")):
            st = "COMPLETE"
        else:
            st = "OPEN" if raw else None
        return {"status": st, "filled": int(s.get("filled_qty") or 0), "avg": s.get("avg_price"),
                "time": s.get("fill_time"), "msg": s.get("status") if st == "REJECTED" else None}

    def cancel(self, c, oid):
        self.ke.cancel(c, oid)


class PaperAdapter:
    """Fills against the live Fyers quote. Marketable orders fill at the touch (buy at ask, sell at
    bid); a limit fills when the touch reaches it. State lives in memory — a restart cancels
    resting paper orders (fills already recorded in the deployment file stay)."""
    def __init__(self, quote, clock=None):
        self.quote = quote                 # fn(fyers_symbol) -> {ltp, bid, ask} | None
        self.clock = clock or now_ist      # practice mode passes the replay clock
        self.book = {}
        self.n = 0

    def _touch(self, k, side):
        q = self.quote(k["symbol"]) or {}
        ltp = q.get("ltp")
        px = q.get("ask") if side == "B" else q.get("bid")
        return float(px) if px else (float(ltp) if ltp else None)

    def price(self, c, k, side, qty, fallback, cushion):
        return self._touch(k, side) or fallback

    def place(self, c, k, side, qty, price, product, tag):
        self.n += 1
        oid = f"P{self.n:05d}"
        self.book[oid] = {"k": k, "side": side, "qty": qty, "px": float(price), "status": "OPEN",
                          "filled": 0, "avg": None}
        self._try(oid)
        return oid, tag

    def _try(self, oid):
        o = self.book[oid]
        if o["status"] != "OPEN":
            return
        t = self._touch(o["k"], o["side"])
        if t is None:
            return
        if (o["side"] == "B" and t <= o["px"]) or (o["side"] == "S" and t >= o["px"]):
            o.update(status="COMPLETE", filled=o["qty"], avg=t)

    def status(self, c, oid):
        o = self.book.get(oid)
        if not o:
            return {"status": "CANCELLED", "filled": 0, "avg": None, "time": None,
                    "msg": "paper order lost in a restart"}
        self._try(oid)
        return {"status": o["status"], "filled": o["filled"], "avg": o["avg"],
                "time": self.clock().strftime("%H:%M:%S"), "msg": None}

    def cancel(self, c, oid):
        o = self.book.get(oid)
        if o and o["status"] == "OPEN":
            o["status"] = "CANCELLED"


# ── the desk ─────────────────────────────────────────────────────────────────────────────
class Desk:
    def __init__(self, hub, quotes, adapters, resolve, clock=now_ist, sleep=time.sleep,
                 fill_wait=FILL_WAIT, dep_dir=None, control=None):
        """hub: BrokerHub-like (get(id) -> account with .ensure() .kind .name .lock .refresh_funds)
        quotes: fn(list[fyers legs]) -> {fyers_symbol: {ltp,bid,ask}}
        adapters: {"kite": KiteAdapter, "kotak": KotakAdapter}
        resolve: fn(account, leg) -> broker contract"""
        self.hub, self.quotes, self.adapters, self.resolve = hub, quotes, adapters, resolve
        self.clock, self.sleep, self.fill_wait = clock, sleep, fill_wait
        self.deps: dict[str, dict] = {}
        self.lock = threading.RLock()
        self._busy: set[str] = set()                 # deployments with a running thread
        self._last_hist: dict[str, float] = {}
        self._last_open: set[str] = set()
        self.dep_dir = Path(dep_dir) if dep_dir else DEP_DIR      # practice sessions use their own folder
        self.control_path = Path(control) if control else CONTROL
        self.dep_dir.mkdir(parents=True, exist_ok=True)
        (self.dep_dir / "history").mkdir(exist_ok=True)
        self._load()

    # ── persistence ──
    def _save(self, d):
        d["updated"] = self.clock().strftime("%Y-%m-%d %H:%M:%S")
        p = self.dep_dir / f"{d['id']}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)

    def _load(self):
        for p in sorted(self.dep_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("bad deployment file %s: %s", p.name, e)
                continue
            if d.get("status") in ("placing", "closing"):
                # the thread that was running this died with the process — never resume blindly
                d["status"] = "interrupted"
                self._log(d, "service restarted while orders were in flight — check positions, "
                             "then square off or leave as is")
                self._save(d)
            self.deps[d["id"]] = d

    def _log(self, d, msg):
        d.setdefault("log", []).append(f"{self.clock():%H:%M:%S} {msg}")
        log.info("[%s %s] %s", d.get("name"), d.get("broker"), msg)

    # ── control ──
    def armed(self) -> dict:
        try:
            return json.loads(self.control_path.read_text(encoding="utf-8"))
        except Exception:
            return {"armed": False}

    def set_armed(self, on: bool, by: str = "page"):
        C.DATA.mkdir(parents=True, exist_ok=True)
        c = {"armed": bool(on), "by": by, "at": self.clock().strftime("%Y-%m-%d %H:%M:%S")}
        self.control_path.write_text(json.dumps(c), encoding="utf-8")
        return c

    # ── book ──
    @staticmethod
    def leg_book(d, leg_id):
        """(net qty signed +long, buy value, sell value) from OUR fills."""
        net, bv, sv = 0, 0.0, 0.0
        for o in d["orders"]:
            if o["leg"] != leg_id or not o.get("filled"):
                continue
            q, px = int(o["filled"]), float(o.get("avg") or 0)
            if o["side"] == "B":
                net += q
                bv += q * px
            else:
                net -= q
                sv += q * px
        return net, bv, sv

    def view(self, d) -> dict:
        """Deployment + live per-leg position and P&L (total = realised + unrealised)."""
        q = self.quotes([l for l in d["legs"]]) if d["legs"] else {}
        legs, total, open_qty = [], 0.0, 0
        for l in d["legs"]:
            net, bv, sv = self.leg_book(d, l["id"])
            ltp = (q.get(l["fyers_symbol"]) or {}).get("ltp")
            pnl = (sv - bv + net * ltp) if ltp is not None else (sv - bv if net == 0 else None)
            if pnl is None or total is None:
                total = None                 # one open leg without a price -> MTM UNKNOWN, never a partial sum
            else:
                total += pnl
            open_qty += abs(net)
            filled_in = sum(int(o.get("filled") or 0) for o in d["orders"] if o["leg"] == l["id"] and o["side"] == l["side"])
            avg_in = (sum(int(o.get("filled") or 0) * float(o.get("avg") or 0) for o in d["orders"]
                          if o["leg"] == l["id"] and o["side"] == l["side"]) / filled_in) if filled_in else None
            legs.append({**l, "net": net, "avg": avg_in, "ltp": ltp, "pnl": pnl})
        ce, pe = self.short_premium(legs)
        tot = (ce or 0) + (pe or 0)
        imb = (100.0 * (ce - pe) / tot) if (ce is not None and pe is not None and tot) else None
        return {**{k: v for k, v in d.items() if k != "legs"}, "legs": legs, "mtm": total,
                "open_qty": open_qty, "short_ce": ce, "short_pe": pe, "imbalance": imb}

    @staticmethod
    def short_premium(legs):
        """Rupee premium currently held SHORT on calls and on puts (|net short qty| x LTP), netted
        per contract first — a strike sold on one leg and bought back on another counts as flat.
        None if a short contract has no price (unknown, never a partial number)."""
        net = {}
        for l in legs:
            k = (l["index"], l["expiry"], float(l["strike"]), l["type"])
            n = net.setdefault(k, {"qty": 0, "ltp": None})
            n["qty"] += l["net"]
            if l.get("ltp") is not None:
                n["ltp"] = float(l["ltp"])
        ce = pe = 0.0
        for (idx, exp, strike, typ), n in net.items():
            if n["qty"] >= 0:
                continue
            if n["ltp"] is None:
                return None, None
            v = -n["qty"] * n["ltp"]
            if typ == "CE":
                ce += v
            else:
                pe += v
        return ce, pe

    def list(self, include_closed_days: int = 1) -> list:
        cut = (self.clock() - dt.timedelta(days=include_closed_days)).strftime("%Y-%m-%d")
        with self.lock:
            ds = [d for d in self.deps.values() if d["status"] not in ("closed", "failed")
                  or d.get("updated", "")[:10] >= cut]
        return [self.view(d) for d in sorted(ds, key=lambda x: x["created"], reverse=True)]

    # ── deploy ──
    def deploy(self, req: dict) -> list:
        """req = {name, strategy_id, legs:[{index,expiry,strike,type,side,lots,price,symbol}],
                  brokers:{account_id: multiplier}, mode: 'market'|'limit', product: 'NRML'|'MIS',
                  sl: rupees|None, target: rupees|None, exit_time: 'HH:MM'|None}"""
        brokers_req = {k: int(v) for k, v in (req.get("brokers") or {}).items() if int(v) > 0}
        if any(self.hub.get(a).kind != "paper" for a in brokers_req) and not self.armed().get("armed"):
            raise ValueError("Live trading is NOT armed — arm it on the page first (Paper works disarmed)")
        if not market_open(self.clock()):
            raise ValueError("Market is closed (orders only 09:15-15:30 IST, Mon-Fri)")
        name = (req.get("name") or "").strip()
        if not name:
            raise ValueError("Give the strategy a name")
        legs = req.get("legs") or []
        if not 1 <= len(legs) <= MAX_LEGS:
            raise ValueError(f"1..{MAX_LEGS} legs")
        brokers = {k: int(v) for k, v in (req.get("brokers") or {}).items() if int(v) > 0}
        if not brokers:
            raise ValueError("Tick at least one broker")
        mode = req.get("mode", "market")
        product = req.get("product", "NRML")
        if mode not in ("market", "limit") or product not in ("NRML", "MIS"):
            raise ValueError("bad mode/product")
        for l in legs:
            if l.get("side") not in ("B", "S") or l.get("type") not in ("CE", "PE") \
                    or l.get("index") not in C.INDICES or int(l.get("lots", 0)) < 1:
                raise ValueError(f"bad leg {l}")
            if mode == "limit" and not float(l.get("price") or 0) > 0:
                raise ValueError("limit mode needs a price on every leg")
            for aid, m in brokers.items():
                if int(l["lots"]) * m > MAX_LOTS:
                    raise ValueError(f"{int(l['lots']) * m} lots on one leg is over the {MAX_LOTS}-lot cap")
        ids = []
        for aid, mult in brokers.items():
            acct = self.hub.get(aid)
            d = self._new(name, req, acct, mult, mode, product, legs)
            ids.append(d["id"])
            self._spawn(d, self._run_entry)
        return ids

    def quick(self, req: dict) -> str:
        """Keyboard / Trade-mode order. req = {broker, product, legs:[{index,expiry,strike,type,side,lots,symbol}],
        deployment?: id}. With `deployment` the order is an ADJUSTMENT of that strategy (its card, MTM and
        imbalance include it); without, it goes to today's "Quick · <broker>" book. Returns the deployment id."""
        target = self.deps.get(req.get("deployment")) if req.get("deployment") else None
        if req.get("deployment") and target is None:
            raise ValueError("that strategy no longer exists — pick it again in 'into:'")
        aid = target["broker"] if target else req.get("broker")
        acct = self.hub.get(aid)
        if acct.kind != "paper" and not self.armed().get("armed"):
            raise ValueError("Live trading is NOT armed — arm it first, or trade on Paper")
        if not market_open(self.clock()):
            raise ValueError("Market is closed (orders only 09:15-15:30 IST, Mon-Fri)")
        legs = req.get("legs") or []
        if not 1 <= len(legs) <= 4:
            raise ValueError("1..4 legs per quick order")
        for l in legs:
            if l.get("side") not in ("B", "S") or l.get("type") not in ("CE", "PE") \
                    or l.get("index") not in C.INDICES or not 1 <= int(l.get("lots", 0)) <= MAX_LOTS:
                raise ValueError(f"bad leg {l}")
        product = target["product"] if target else req.get("product", "NRML")
        today = self.clock().strftime("%Y-%m-%d")
        with self.lock:
            d = target or next((x for x in self.deps.values() if x.get("quick") and x["broker"] == aid
                                and x["created"][:10] == today and x["product"] == product), None)
        if target is not None and target["status"] in ("closed", "failed"):
            target["status"] = "open"                         # adding to a flat strategy re-opens it
        if d is None:
            d = self._new(f"Quick · {acct.name}", {"name": "quick"}, acct, 1, "market", product, [])
            d["quick"] = True
            d["status"] = "open"
        items = []
        for l in legs:
            key = (l["index"], l["expiry"], float(l["strike"]), l["type"])
            leg = next((x for x in d["legs"] if (x["index"], x["expiry"], float(x["strike"]), x["type"]) == key), None)
            if leg is None:
                leg = {"id": len(d["legs"]), "index": l["index"], "expiry": l["expiry"], "strike": float(l["strike"]),
                       "type": l["type"], "side": l["side"], "lots": int(l["lots"]), "qty": None,
                       "fyers_symbol": l.get("symbol"), "price": None, "contract": None}
                d["legs"].append(leg)
            items.append((leg["id"], l["side"], int(l["lots"])))
        self._log(d, ("adjust: " if target else "quick: ") + ", ".join(f"{'BUY' if s == 'B' else 'SELL'} {lt}x "
                                            f"{d['legs'][i]['strike']:.0f}{d['legs'][i]['type']}" for i, s, lt in items))
        self._save(d)
        self._spawn(d, self._run_adhoc, items)
        return d["id"]

    def _run_adhoc(self, d, items):
        """One quick order: BUYs first, then SELLs; a BUY that does not fill blocks the SELLs."""
        acct = self.hub.get(d["broker"])
        with acct.lock:
            d["status"] = "placing"
            self._resolve_all(d, acct)
            for lid, side, lots in sorted(items, key=lambda x: 0 if x[1] == "B" else 1):
                leg = d["legs"][lid]
                qty = lots * int(leg["contract"]["lot"])
                got = self._fill(d, acct, leg, side, qty, "entry", ENTRY_CUSHIONS)
                if got < qty:
                    self._log(d, f"{'BUY' if side == 'B' else 'SELL'} {leg['contract']['symbol']} filled {got}/{qty}"
                                 + (" — remaining SELLs not sent" if side == "B" else ""))
                    if side == "B":
                        break
            d["status"] = "open" if any(self.leg_book(d, l["id"])[0] for l in d["legs"]) else "closed"
            self._save(d)
            acct.refresh_funds()

    def _new_id(self, acct_id):
        used = {d.get("tag") for d in self.deps.values() if d["broker"] == acct_id}
        while True:
            i = secrets.token_hex(3)
            tag = "sb" + i[:3]          # Kotak: prefix + 9 = 14 chars (same as vwskd); Kite uses sb+6
            if tag not in used and i not in self.deps:
                return i, tag

    def _new(self, name, req, acct, mult, mode, product, legs):
        i, ktag = self._new_id(acct.id)
        d = {"id": i, "name": name, "strategy_id": req.get("strategy_id"), "broker": acct.id,
             "broker_name": acct.name, "kind": acct.kind, "mult": mult, "mode": mode,
             "product": product, "tag": ktag if acct.kind == "kotak" else "sb" + i,
             "created": self.clock().strftime("%Y-%m-%d %H:%M:%S"), "status": "placing",
             "sl": req.get("sl"), "target": req.get("target"), "exit_time": req.get("exit_time"),
             "exit_reason": None, "orders": [], "log": [],
             "legs": [{"id": n, "index": l["index"], "expiry": l["expiry"], "strike": float(l["strike"]),
                       "type": l["type"], "side": l["side"], "lots": int(l["lots"]) * mult,
                       "qty": None, "fyers_symbol": l.get("symbol"), "price": l.get("price"),
                       "contract": None} for n, l in enumerate(legs)]}
        with self.lock:
            self.deps[i] = d
        self._log(d, f"deploy {len(legs)} legs x{mult} on {acct.name} ({mode}, {product})")
        self._save(d)
        return d

    def _spawn(self, d, fn, *a):
        with self.lock:
            if d["id"] in self._busy:
                raise ValueError("this deployment is already busy placing/closing orders")
            self._busy.add(d["id"])

        def run():
            try:
                fn(d, *a)
            except Exception as e:
                self._log(d, f"ERROR {type(e).__name__}: {e}")
                d["status"] = "error"
                self._save(d)
            finally:
                with self.lock:
                    self._busy.discard(d["id"])
        threading.Thread(target=run, daemon=True, name=f"SB-{d['id']}").start()

    # ── execution ──
    def _fallback(self, leg, side):
        q = self.quotes([leg]).get(leg["fyers_symbol"]) or {}
        ltp = q.get("ltp")
        if not ltp:
            return None
        return ltp * (1.10 if side == "B" else 0.90)

    def _fill(self, d, acct, leg, side, qty, purpose, cushions, limit_price=None) -> int:
        """Fill `qty` of one leg; returns the quantity actually filled. Slices big orders,
        chases (marketable mode) or rests (limit price)."""
        ad = self.adapters[acct.kind]
        c = acct.ensure()
        k = leg["contract"]
        lot = int(k["lot"])
        slice_q = SLICE_LOTS[leg["index"]] * lot
        done = 0
        while done < qty:
            want = min(slice_q, qty - done)
            got = self._fill_slice(d, acct, ad, c, leg, side, want, purpose, cushions, limit_price)
            done += got
            if got < want:
                break
        return done

    def _fill_slice(self, d, acct, ad, c, leg, side, qty, purpose, cushions, limit_price):
        filled = 0
        rounds = [None] if limit_price else list(cushions)
        for r, cushion in enumerate(rounds):
            left = qty - filled
            if left <= 0:
                break
            if limit_price:
                px = float(limit_price)
            else:
                px = ad.price(c, leg["contract"], side, left, self._fallback(leg, side), cushion)
                if not px:
                    self._log(d, f"leg {leg['id']}: no price from the book or the feed — not sending")
                    break
            o = {"leg": leg["id"], "purpose": purpose, "side": side, "qty": left, "price": px,
                 "oid": None, "tag": None, "status": "SENDING", "filled": 0, "avg": None,
                 "time": self.clock().strftime("%H:%M:%S"), "msg": None, "round": r + 1}
            d["orders"].append(o)
            self._save(d)
            try:
                o["oid"], o["tag"] = ad.place(c, leg["contract"], side, left, px, d["product"], d["tag"])
                o["status"] = "OPEN"
            except Exception as e:
                o["status"], o["msg"] = "REJECTED", f"{type(e).__name__}: {e}"[:300]
                self._log(d, f"{purpose} {side} {leg['contract']['symbol']} x{left} @ {px}: {o['msg']}")
                self._save(d)
                break
            self._save(d)
            if limit_price:
                st = self._wait(ad, c, o, self.fill_wait)
                if st["status"] != "COMPLETE":
                    self._log(d, f"limit order {o['oid']} resting @ {px}")
                return int(o["filled"] or 0) + filled if st["status"] == "COMPLETE" else filled
            st = self._wait(ad, c, o, self.fill_wait)
            if st["status"] != "COMPLETE" and st["status"] not in ("REJECTED", "CANCELLED"):
                ad.cancel(c, o["oid"])
                st = self._wait(ad, c, o, 3.0, until_final=True)
            filled += int(o["filled"] or 0)
            if st["status"] == "REJECTED":
                self._log(d, f"{purpose} {side} {leg['contract']['symbol']} rejected: {o.get('msg')}")
                break
            self._save(d)
        return filled

    def _wait(self, ad, c, o, secs, until_final=False):
        end = time.monotonic() + secs
        st = {"status": None}
        while True:
            st = ad.status(c, o["oid"])
            if st.get("status"):
                o["status"] = st["status"]
            o["filled"] = st.get("filled") or o.get("filled") or 0
            if st.get("avg") is not None:
                o["avg"] = float(st["avg"])
            if st.get("msg"):
                o["msg"] = st["msg"]
            if st.get("status") in FINAL:
                return st
            if time.monotonic() >= end:
                return st
            self.sleep(0.6)

    def _resolve_all(self, d, acct):
        for leg in d["legs"]:
            if not leg["contract"]:
                k = self.resolve(acct, leg)
                leg["contract"] = k
                leg["qty"] = leg["lots"] * int(k["lot"])
        self._save(d)

    def _run_entry(self, d):
        acct = self.hub.get(d["broker"])
        with acct.lock:
            self._resolve_all(d, acct)
            limit = d["mode"] == "limit"
            buys = [l for l in d["legs"] if l["side"] == "B"]
            sells = [l for l in d["legs"] if l["side"] == "S"]
            for leg in buys:
                got = self._fill(d, acct, leg, "B", leg["qty"], "entry", ENTRY_CUSHIONS,
                                 leg["price"] if limit else None)
                if got < leg["qty"] and not limit:
                    d["status"] = "failed" if not any(o.get("filled") for o in d["orders"]) else "partial"
                    self._log(d, f"hedge leg {leg['contract']['symbol']} filled {got}/{leg['qty']} — "
                                 f"NO sell legs sent (never leave a naked short)")
                    self._save(d)
                    return
            for leg in sells:
                got = self._fill(d, acct, leg, "S", leg["qty"], "entry", ENTRY_CUSHIONS,
                                 leg["price"] if limit else None)
                if got < leg["qty"] and not limit:
                    d["status"] = "partial"
                    self._log(d, f"sell leg {leg['contract']['symbol']} filled {got}/{leg['qty']} — stopped")
                    self._save(d)
                    return
            d["status"] = "pending" if limit and any(o["status"] == "OPEN" for o in d["orders"]) else "open"
            self._log(d, "all legs placed" if d["status"] == "pending" else "all legs filled")
            self._save(d)
            acct.refresh_funds()

    def _run_exit(self, d, leg_ids, reason):
        acct = self.hub.get(d["broker"])
        with acct.lock:
            d["status"] = "closing"
            d["exit_reason"] = reason
            self._log(d, f"square-off ({reason})")
            self._save(d)
            ad = self.adapters[acct.kind]
            c = acct.ensure()
            for o in d["orders"]:                    # pull resting entry orders first
                if o["status"] == "OPEN" and o["purpose"] == "entry" and o["oid"] and \
                        (leg_ids is None or o["leg"] in leg_ids):
                    ad.cancel(c, o["oid"])
                    self._wait(ad, c, o, 3.0, until_final=True)
            legs = [l for l in d["legs"] if leg_ids is None or l["id"] in leg_ids]
            shorts = [l for l in legs if self.leg_book(d, l["id"])[0] < 0]
            longs = [l for l in legs if self.leg_book(d, l["id"])[0] > 0]
            ok = True
            for leg in shorts + longs:               # shorts out first, then the hedges
                net = self.leg_book(d, leg["id"])[0]
                side = "B" if net < 0 else "S"
                got = self._fill(d, acct, leg, side, abs(net), "exit", EXIT_CUSHIONS)
                if got < abs(net):
                    ok = False
                    self._log(d, f"exit {leg['contract']['symbol']} filled {got}/{abs(net)} — STILL OPEN")
                    if side == "B":
                        break                         # never sell a hedge while its short is open
            left = sum(abs(self.leg_book(d, l["id"])[0]) for l in d["legs"])
            d["status"] = "closed" if left == 0 else ("open" if ok else "exit_failed")
            self._log(d, "flat" if left == 0 else f"{left} qty still open")
            self._save(d)
            acct.refresh_funds()

    # ── user actions ──
    def square_off(self, dep_id, leg_id=None, reason="manual"):
        d = self.deps.get(dep_id)
        if not d:
            raise ValueError("unknown deployment")
        if not market_open(self.clock()):
            raise ValueError("Market is closed")
        self._spawn(d, self._run_exit, None if leg_id is None else [int(leg_id)], reason)

    def square_off_all(self, reason="kill switch"):
        n = 0
        for d in list(self.deps.values()):
            if d["status"] not in ("closed", "failed") and d["id"] not in self._busy:
                if any(self.leg_book(d, l["id"])[0] for l in d["legs"]) or \
                        any(o["status"] == "OPEN" for o in d["orders"]):
                    self.square_off(d["id"], reason=reason)
                    n += 1
        return n

    def cancel_pending(self, dep_id):
        d = self.deps[dep_id]
        acct = self.hub.get(d["broker"])
        ad, c = self.adapters[acct.kind], acct.ensure()
        for o in d["orders"]:
            if o["status"] == "OPEN" and o["oid"]:
                ad.cancel(c, o["oid"])
                self._wait(ad, c, o, 3.0, until_final=True)
        if not any(self.leg_book(d, l["id"])[0] for l in d["legs"]):
            d["status"] = "closed"
        self._log(d, "pending orders cancelled")
        self._save(d)

    def set_risk(self, dep_id, sl=None, target=None, exit_time=None):
        d = self.deps[dep_id]
        d["sl"], d["target"], d["exit_time"] = sl, target, exit_time
        self._log(d, f"risk set: SL {sl} target {target} exit {exit_time}")
        self._save(d)

    # ── monitor: resting orders, SL / target / exit time ──
    def hist_path(self, dep_id):
        return self.dep_dir / "history" / f"{dep_id}.jsonl"

    def _sample(self, d, v, t):
        """Append one {t, mtm, ce, pe, imb} point (every HIST_EVERY s while a position is open)."""
        now = t.timestamp()                  # the desk's clock: real time live, replay time in practice
        if now - self._last_hist.get(d["id"], 0) < HIST_EVERY:
            return
        self._last_hist[d["id"]] = now
        rec = {"t": t.strftime("%Y-%m-%d %H:%M:%S"), "mtm": v["mtm"], "ce": v["short_ce"],
               "pe": v["short_pe"], "imb": v["imbalance"], "open": v["open_qty"]}
        with open(self.hist_path(d["id"]), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def history(self, dep_id, date: str | None = None) -> list:
        out = []
        try:
            with open(self.hist_path(dep_id), encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if date is None or r["t"][:10] == date:
                        out.append(r)
        except FileNotFoundError:
            pass
        return out

    def tick(self):
        t = self.clock()
        for d in list(self.deps.values()):
            if d["status"] in ("closed", "failed") and d["id"] not in self._last_open:
                continue
            try:
                if market_open(t) or d["id"] in self._last_open:
                    v0 = self.view(d)
                    if v0["open_qty"] or d["id"] in self._last_open:
                        self._sample(d, v0, t)
                    if v0["open_qty"]:
                        self._last_open.add(d["id"])
                    else:
                        self._last_open.discard(d["id"])        # one last point at flat, then stop
            except Exception as e:
                log.warning("history %s: %s", d["id"], e)
            if d["status"] in ("closed", "failed") or d["id"] in self._busy:
                continue
            try:
                if d["status"] == "pending":
                    self._refresh_resting(d)
                v = self.view(d)
                if not v["open_qty"] or not market_open(t):
                    continue
                mtm = v["mtm"]
                if d.get("exit_time") and t.strftime("%H:%M") >= d["exit_time"]:
                    self.square_off(d["id"], reason=f"exit time {d['exit_time']}")
                    continue
                if mtm is None:              # no price for an open leg -> SL/target cannot be judged
                    continue
                if d.get("sl") and mtm <= -abs(float(d["sl"])):
                    self.square_off(d["id"], reason=f"stop-loss ₹{d['sl']} hit (MTM {mtm:,.0f})")
                elif d.get("target") and mtm >= abs(float(d["target"])):
                    self.square_off(d["id"], reason=f"target ₹{d['target']} hit (MTM {mtm:,.0f})")
            except Exception as e:
                log.warning("tick %s: %s", d["id"], e)

    def _refresh_resting(self, d):
        acct = self.hub.get(d["broker"])
        ad, c = self.adapters[acct.kind], acct.ensure()
        changed = False
        for o in d["orders"]:
            if o["status"] == "OPEN" and o["oid"]:
                before = (o["status"], o["filled"])
                self._wait(ad, c, o, 0)
                changed |= before != (o["status"], o["filled"])
        if not any(o["status"] == "OPEN" for o in d["orders"]):
            d["status"] = "open"
            changed = True
        if changed:
            self._save(d)

    def run_monitor(self, every=1.0):
        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:
                    log.warning("monitor: %s", e)
                time.sleep(every)
        threading.Thread(target=loop, daemon=True, name="SB-monitor").start()
