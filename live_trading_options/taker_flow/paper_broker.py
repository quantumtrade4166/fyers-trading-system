"""
Taker Flow L/S — paper broker on Delta Exchange India prices, with Delta's charges.

Fills  : every order is a TAKER order that walks Delta's live L2 book
         (/v2/l2orderbook) level by level — real spread + depth slippage.
         An order bigger than the visible book fills the remainder at the last
         level and is flagged (thin_book) in the trade record.
Fees   : product taker_commission_rate (0.05% futures) on notional, + 18% GST.
Funding: Delta ticker funding_rate is % per 8h; accrued every hour at rate/8 on
         the position's notional at mark. Long pays a positive rate, short receives.
Units  : Delta contracts (contract_value x coin). Orders are whole contracts.

The ledger is the single source of truth and is saved after every change.
"""

import json
import math
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from .flow_signal import DELTA, _get

GST = 0.18


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Position:
    symbol: str                 # Delta symbol, e.g. DOGEUSD
    underlying: str
    product_id: int
    contract_value: float
    contracts: int = 0          # signed: + long, - short
    avg_price: float = 0.0      # average entry of the open contracts
    mark: float = 0.0


@dataclass
class Ledger:
    capital: float
    realized: float = 0.0       # price P&L of closed contracts
    fees: float = 0.0           # trading fees excl. GST
    gst: float = 0.0
    slippage: float = 0.0       # cost vs mid at the moment of the order (info, already inside price P&L)
    funding: float = 0.0        # + = paid by us, - = received
    # the same, split by book (long = positive contracts, short = negative)
    realized_long: float = 0.0
    realized_short: float = 0.0
    charges_long: float = 0.0   # fee + GST of orders that opened/added/closed LONG contracts
    charges_short: float = 0.0
    funding_long: float = 0.0
    funding_short: float = 0.0
    positions: dict = field(default_factory=dict)   # underlying -> Position
    last_funding_hour: str = ""
    last_rebalance: str = ""
    started: str = ""

    # ── accounting ──
    def unrealized(self) -> float:
        return sum((p.mark - p.avg_price) * p.contracts * p.contract_value
                   for p in self.positions.values() if p.contracts)

    def equity(self) -> float:
        return self.capital + self.realized + self.unrealized() - self.fees - self.gst - self.funding

    def book(self, side: str) -> dict:
        """P&L of one book. side: 'long' or 'short'."""
        sign = 1 if side == "long" else -1
        pos = [p for p in self.positions.values() if p.contracts * sign > 0]
        unreal = sum((p.mark - p.avg_price) * p.contracts * p.contract_value for p in pos)
        realized = self.realized_long if side == "long" else self.realized_short
        charges = self.charges_long if side == "long" else self.charges_short
        funding = self.funding_long if side == "long" else self.funding_short
        price = realized + unreal
        return {"net": round(price - charges - funding, 2), "price": round(price, 2),
                "realized": round(realized, 2), "unrealized": round(unreal, 2),
                "charges": round(charges, 2), "funding": round(funding, 2), "positions": len(pos),
                "notional": round(sum(abs(p.contracts) * p.contract_value * p.mark for p in pos), 2)}

    def gross_exposure(self) -> float:
        return sum(abs(p.contracts) * p.contract_value * p.mark for p in self.positions.values())

    # ── persistence ──
    def save(self, path: Path) -> None:
        d = asdict(self)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        for _ in range(5):                       # a reader may hold the file (Windows)
            try:
                tmp.replace(path)
                return
            except PermissionError:
                time.sleep(0.2)
        path.write_text(json.dumps(d, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, capital: float) -> "Ledger":
        if not path.exists():
            return cls(capital=capital, started=utc_now())
        d = json.loads(path.read_text(encoding="utf-8"))
        pos = {k: Position(**v) for k, v in d.pop("positions", {}).items()}
        return cls(positions=pos, **d)


# ── order book ───────────────────────────────────────────────────────────
def l2(symbol: str) -> dict:
    return _get(f"{DELTA}/v2/l2orderbook/{symbol}")["result"]


def walk_book(book: dict, contracts: int) -> tuple[float, float, bool]:
    """Average fill price for a taker order of `contracts` (+ buy / - sell),
    the book mid, and whether the visible book ran out."""
    side = "sell" if contracts > 0 else "buy"          # a buy lifts the asks
    levels = [(float(l["price"]), float(l["size"])) for l in book.get(side) or []]
    bids, asks = book.get("buy") or [], book.get("sell") or []
    if not levels or not bids or not asks:
        raise RuntimeError("empty order book")
    mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
    need, cost, last = abs(contracts), 0.0, levels[0][0]
    for price, size in levels:
        take = min(need, size)
        cost += take * price
        need -= take
        last = price
        if need <= 0:
            break
    thin = need > 0
    if thin:
        cost += need * last
    return cost / abs(contracts), mid, thin


# ── execution ────────────────────────────────────────────────────────────
def execute(ledger: Ledger, info: dict, underlying: str, delta_contracts: int,
            reason: str) -> dict | None:
    """Fill a paper taker order of delta_contracts (+buy/-sell) and book it."""
    if delta_contracts == 0:
        return None
    sym = info["symbol"]
    pos = ledger.positions.get(underlying) or Position(
        symbol=sym, underlying=underlying, product_id=info["product_id"],
        contract_value=info["contract_value"], mark=info["mark"])
    price, mid, thin = walk_book(l2(sym), delta_contracts)
    cv = pos.contract_value
    notional = abs(delta_contracts) * cv * price
    fee = notional * info["taker_fee"]
    gst = fee * GST
    slip = abs(price - mid) * abs(delta_contracts) * cv

    # realize the part that reduces/closes the open position
    realized = 0.0
    closing = 0
    if pos.contracts and (pos.contracts > 0) != (delta_contracts > 0):
        closing = min(abs(delta_contracts), abs(pos.contracts))
        sign = 1 if pos.contracts > 0 else -1
        realized = (price - pos.avg_price) * closing * cv * sign
    # book attribution: the closing part belongs to the side being closed, the rest
    # to the side being opened/added (a flip splits the order's fee pro rata)
    old_long = pos.contracts > 0
    opening = abs(delta_contracts) - closing
    open_long = delta_contracts > 0
    long_share = ((closing if old_long else 0) + (opening if open_long else 0)) / abs(delta_contracts)
    new = pos.contracts + delta_contracts
    if new == 0:
        pos.avg_price = 0.0
    elif pos.contracts == 0 or (pos.contracts > 0) != (new > 0):
        pos.avg_price = price                          # fresh position or flipped side
    elif abs(new) > abs(pos.contracts):                # adding in the same direction
        pos.avg_price = (pos.avg_price * abs(pos.contracts) + price * abs(delta_contracts)) / abs(new)
    pos.contracts = new
    pos.mark = info["mark"]
    ledger.positions[underlying] = pos
    if new == 0:
        ledger.positions.pop(underlying, None)
    ledger.realized += realized
    if old_long:
        ledger.realized_long += realized
    else:
        ledger.realized_short += realized
    ledger.charges_long += (fee + gst) * long_share
    ledger.charges_short += (fee + gst) * (1 - long_share)
    ledger.fees += fee
    ledger.gst += gst
    ledger.slippage += slip
    return {"ts": utc_now(), "symbol": sym, "underlying": underlying,
            "side": "BUY" if delta_contracts > 0 else "SELL", "contracts": abs(delta_contracts),
            "price": price, "mid": mid, "notional": round(notional, 2),
            "fee": round(fee, 4), "gst": round(gst, 4), "slippage": round(slip, 4),
            "realized": round(realized, 4), "position_after": new, "thin_book": thin,
            "reason": reason}


def accrue_funding(ledger: Ledger, delta: dict, hour_key: str) -> float:
    """One hour of funding on every open position (rate/8 of the 8h rate)."""
    if ledger.last_funding_hour == hour_key:
        return 0.0
    total = 0.0
    for u, p in ledger.positions.items():
        info = delta.get(u)
        if not info or not p.contracts:
            continue
        rate_h = info["funding_rate_8h_pct"] / 100 / 8
        amt = p.contracts * p.contract_value * info["mark"] * rate_h
        total += amt
        if p.contracts > 0:
            ledger.funding_long += amt
        else:
            ledger.funding_short += amt
    ledger.funding += total
    ledger.last_funding_hour = hour_key
    return total


def split_existing(ledger: Ledger, trades: list[dict], delta: dict, funded_hours: int) -> bool:
    """One-off: a ledger saved before the per-book fields existed gets them rebuilt
    from its own trade log. Funding so far = funded_hours x the long book's current
    hourly funding (Delta rates); the short book takes the rest, so the total stays
    exact. It is cents — the positions are hours old. Returns True if done."""
    if not ledger.fees or (ledger.charges_long or ledger.charges_short):
        return False
    for t in trades:
        after, n = t["position_after"], t["contracts"]
        before = after - n if t["side"] == "BUY" else after + n
        closing = min(n, abs(before)) if before and (before > 0) != (t["side"] == "BUY") else 0
        open_long = t["side"] == "BUY"
        long_share = ((closing if before > 0 else 0) + ((n - closing) if open_long else 0)) / n
        ch = t["fee"] + t["gst"]
        ledger.charges_long += ch * long_share
        ledger.charges_short += ch * (1 - long_share)
        if t["realized"]:
            if before > 0:
                ledger.realized_long += t["realized"]
            else:
                ledger.realized_short += t["realized"]
    if ledger.funding:
        long_h = sum(p.contracts * p.contract_value * delta[u]["mark"] * delta[u]["funding_rate_8h_pct"] / 100 / 8
                     for u, p in ledger.positions.items() if p.contracts > 0 and u in delta)
        ledger.funding_long = long_h * funded_hours
        ledger.funding_short = ledger.funding - ledger.funding_long
    return True


def target_contracts(equity: float, lev: float, weight: float, info: dict) -> int:
    usd = equity * lev * weight
    per = info["contract_value"] * info["mark"]
    return int(math.copysign(round(abs(usd) / per), usd)) if per > 0 else 0
