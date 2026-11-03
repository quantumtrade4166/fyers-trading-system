"""
cepe_imbalance/book.py — PAPER book per underlying: fills on live bid/ask, MTM, CE/PE imbalance.
================================================================================================

Fills: SELL at best bid, BUY at best ask (fallback LTP when the side is empty). No broker, ever.
Quantity is in LOTS; rupees = price x lots x lot size x multiplier (GOLD x100, GOLDM x0.1).

CE/PE imbalance (owner's rules, same as Strategy Builder / MCX terminal, 25-Oct-2026):
  only CURRENT net-short option contracts count (longs/hedges ignored);
  value = premium x units, premium = LTP kept inside the live bid/ask (market.Quote.mark);  imbalance % = (CE value - PE value) / (CE value + PE value) x 100
  -> None when there are no shorts.

State is saved after every fill (atomic) to data/books/{date}/{name}.json; every fill is appended to
data/books/{date}/{name}_trades.jsonl. A restart on the same day reloads the book.
"""
import datetime as dt
import json
import os
import threading
import time
from dataclasses import asdict, dataclass

from live_trading_options.cepe_imbalance import config as C

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def today() -> str:
    return dt.datetime.now(IST).date().isoformat()


@dataclass
class Position:
    key: str
    symbol: str
    kind: str                # CE | PE | FUT
    strike: float
    expiry: str
    unit_value: float        # rupees per 1 point per lot (lot x mult)
    lots: int = 0            # signed: + long, - short
    avg: float = 0.0         # average price of the open lots
    realized: float = 0.0    # rupees

    def unrealized(self, ltp: float) -> float:
        return (ltp - self.avg) * self.lots * self.unit_value if self.lots and ltp else 0.0


class PaperBook:
    def __init__(self, name: str, market, date: str | None = None, persist: bool = True):
        self.name = name
        self.market = market
        self.date = date or today()
        self.persist = persist
        self.positions: dict[str, Position] = {}
        self.trades: list[dict] = []
        self.lock = threading.RLock()
        if persist:
            self._load()

    # ── paths ───────────────────────────────────────────────────────────────
    def _dir(self):
        d = C.DATA / "books" / self.date
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load(self):
        p = C.DATA / "books" / self.date / f"{self.name}.json"
        if not p.exists():
            return
        doc = json.loads(p.read_text(encoding="utf-8"))
        self.positions = {k: Position(**v) for k, v in doc.get("positions", {}).items()}
        tp = p.with_name(f"{self.name}_trades.jsonl")
        if tp.exists():
            self.trades = [json.loads(x) for x in tp.read_text(encoding="utf-8").splitlines() if x.strip()]

    def _save(self, trade: dict | None):
        if not self.persist:
            return
        d = self._dir()
        p = d / f"{self.name}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"name": self.name, "date": self.date, "saved_at": time.time(),
                                   "positions": {k: asdict(v) for k, v in self.positions.items()}}, indent=1),
                       encoding="utf-8")
        os.replace(tmp, p)
        if trade:
            with open(d / f"{self.name}_trades.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(trade) + "\n")

    # ── orders ──────────────────────────────────────────────────────────────
    def fill_price(self, key: str, side: str) -> float | None:
        q = self.market.quote(key)
        if not q:
            return None
        px = (q.ask if side == "BUY" else q.bid) or q.ltp
        return px if px and px > 0 else None

    def order(self, contract, side: str, lots: int, reason: str = "", price: float | None = None) -> dict:
        """Paper market order. Returns the trade dict, or {'ok': False, 'error': ...}."""
        side = side.upper()
        if side not in ("BUY", "SELL") or int(lots) <= 0:
            return {"ok": False, "error": f"bad order {side} {lots}"}
        lots = int(lots)
        px = price if price is not None else self.fill_price(contract.key, side)
        if not px:
            return {"ok": False, "error": f"no price for {contract.symbol}"}
        with self.lock:
            p = self.positions.get(contract.key)
            if p is None:
                p = self.positions[contract.key] = Position(
                    key=contract.key, symbol=contract.symbol, kind=contract.kind, strike=contract.strike,
                    expiry=contract.expiry, unit_value=contract.lot * contract.mult)
            signed = lots if side == "BUY" else -lots
            before = p.lots
            if before == 0 or (before > 0) == (signed > 0):              # open / add
                p.avg = (p.avg * abs(before) + px * lots) / (abs(before) + lots)
                p.lots = before + signed
            else:                                                       # reduce / close / flip
                closing = min(abs(before), lots)
                sign = 1 if before > 0 else -1
                p.realized += (px - p.avg) * closing * sign * p.unit_value
                p.lots = before + signed
                if p.lots == 0:
                    p.avg = 0.0
                elif (p.lots > 0) != (before > 0):                       # flipped through zero
                    p.avg = px
            q = self.market.quote(contract.key)
            t = {"ok": True, "ts": time.time(), "time": dt.datetime.now(IST).strftime("%H:%M:%S.%f")[:-3],
                 "name": self.name, "key": contract.key, "symbol": contract.symbol, "kind": contract.kind,
                 "strike": contract.strike, "expiry": contract.expiry, "side": side, "lots": lots, "price": px,
                 "ltp": q.ltp if q else None, "bid": q.bid if q else None, "ask": q.ask if q else None,
                 "pos_after": p.lots, "reason": reason}
            self.trades.append(t)
            self._save(t)
            return t

    def exit_all(self, reason: str = "exit all") -> list:
        """Close every open position: shorts (buy-backs) first, then longs."""
        out = []
        with self.lock:
            opens = sorted((p for p in self.positions.values() if p.lots), key=lambda p: p.lots)
            for p in opens:
                c = self.market.u.by_key.get(p.key)
                if c is None:
                    out.append({"ok": False, "error": f"unknown contract {p.key}"})
                    continue
                out.append(self.order(c, "BUY" if p.lots < 0 else "SELL", abs(p.lots), reason))
        return out

    # ── numbers ─────────────────────────────────────────────────────────────
    def mtm(self) -> dict:
        with self.lock:
            real = sum(p.realized for p in self.positions.values())
            unreal = sum(p.unrealized(self.market.mark(p.key)) for p in self.positions.values())
        return {"realized": round(real, 2), "unrealized": round(unreal, 2), "total": round(real + unreal, 2)}

    def imbalance(self) -> dict:
        """pct is None until EVERY short leg has a live price: a missing leg counted as 0 made a
        +19% imbalance out of a real -3.5% right after an engine restart (3-Nov-2026, two false Bs)."""
        ce = pe = 0.0
        missing = []
        with self.lock:
            for p in self.positions.values():
                if p.lots < 0 and p.kind in ("CE", "PE"):
                    px = self.market.mark(p.key)
                    if not px or px <= 0:
                        missing.append(f"{p.strike:g} {p.kind}")
                        continue
                    v = px * -p.lots * p.unit_value
                    if p.kind == "CE":
                        ce += v
                    else:
                        pe += v
        tot = ce + pe
        return {"ce_value": round(ce, 2), "pe_value": round(pe, 2), "missing": missing,
                "pct": round((ce - pe) / tot * 100, 2) if tot > 0 and not missing else None}

    def open_positions(self) -> list:
        with self.lock:
            rows = []
            for p in self.positions.values():
                if not p.lots and not p.realized:
                    continue
                ltp = self.market.mark(p.key)
                rows.append({**asdict(p), "ltp": ltp, "unrealized": round(p.unrealized(ltp), 2)})
            return sorted(rows, key=lambda r: (r["expiry"], r["kind"], r["strike"]))
