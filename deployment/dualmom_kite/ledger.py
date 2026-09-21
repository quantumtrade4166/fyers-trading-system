"""
DualMom ledger for the Kite account - same format and guarantees as the Kotak
client ledger (deployment/dualmom_live/ledger.py): append-only, SHA-256 hash
chained, raw broker payload kept, idempotent capture.

ONLY DualMom. Kite's orders()/trades() return the WHOLE account for TODAY only
(the strangle's option orders included). Every record here is filtered to
orders tagged with config.ORDER_TAG, and fills are joined to those orders by
order_id - a fill whose order is not ours is never written.

Kite forgets orders at the end of the day, so capture runs every few minutes in
market hours and once more at 15:40. Missing a day would lose that day's fills.
"""

import csv
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

from deployment.dualmom_kite import config as C
from deployment.dualmom_kite import kite_equity as K
from deployment.dualmom_live import ledger as LK          # pure helpers only

STATE = Path(__file__).resolve().parents[1] / C.STATE_DIR
ROOT = STATE / "ledger"
_lock = threading.RLock()
CHAINS = ("fills.jsonl", "orders.jsonl", "attempts.jsonl", "events.jsonl")


# ── hash chain (identical scheme to the Kotak ledger) ────────────────────────

def _read(name: str) -> list:
    p = ROOT / name
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def _append(name: str, records: list) -> int:
    if not records:
        return 0
    with _lock:
        ROOT.mkdir(parents=True, exist_ok=True)
        existing = _read(name)
        prev = existing[-1]["hash"] if existing else "GENESIS"
        seq = existing[-1]["seq"] if existing else 0
        with (ROOT / name).open("a", encoding="utf-8", newline="\n") as f:
            for rec in records:
                seq += 1
                body = {**rec, "seq": seq, "prev_hash": prev}
                h = hashlib.sha256((prev + LK._canonical(body)).encode("utf-8")).hexdigest()
                body["hash"] = h
                f.write(LK._canonical(body) + "\n")
                prev = h
        return len(records)


def verify(name: str) -> dict:
    prev = "GENESIS"
    recs = _read(name)
    for i, rec in enumerate(recs, 1):
        body = {k: v for k, v in rec.items() if k != "hash"}
        if rec.get("prev_hash") != prev:
            return {"ok": False, "records": len(recs), "broken_at_seq": i,
                    "reason": "prev_hash does not match the previous record"}
        if rec.get("seq") != i:
            return {"ok": False, "records": len(recs), "broken_at_seq": i,
                    "reason": "sequence gap - a record was removed"}
        if hashlib.sha256((prev + LK._canonical(body)).encode("utf-8")).hexdigest() != rec.get("hash"):
            return {"ok": False, "records": len(recs), "broken_at_seq": i,
                    "reason": "record content was altered"}
        prev = rec["hash"]
    return {"ok": True, "records": len(recs), "broken_at_seq": None, "reason": None}


def verify_all() -> dict:
    return {n: verify(n) for n in CHAINS}


def _keys(name: str) -> set:
    return {r.get("key") for r in _read(name)}


def _iso(x):
    if x is None or x == "":
        return None
    if isinstance(x, datetime):
        return x.isoformat()
    s = str(x).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).isoformat()
        except ValueError:
            continue
    return s


def event(kind: str, detail: dict) -> None:
    _append("events.jsonl", [{"type": "event", "key": f"{kind}|{datetime.now().isoformat()}",
                              "kind": kind, "at": datetime.now().isoformat(timespec="seconds"),
                              "account": C.CLIENT_ACCOUNT, **detail}])


# ── run files -> decision context + attempts ─────────────────────────────────

def _run_files():
    return sorted(STATE.glob("run_*.json")) + sorted(STATE.glob("stop_*.json"))


def decision_index() -> dict:
    idx = {}
    for p in _run_files():
        try:
            run = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        sig = run.get("signal") or {}
        weights = {h.get("symbol"): h.get("weight") for h in sig.get("holdings", [])}
        for o in run.get("orders") or []:
            ctx = {"run_id": p.stem, "signal_date": sig.get("date") or run.get("month_signal_date"),
                   "decision_mark": o.get("mark"), "limit_price": o.get("limit"),
                   "target_qty": o.get("target_qty"), "current_qty": o.get("current_qty"),
                   "target_weight": weights.get(o.get("symbol")), "reason": o.get("reason")}
            if o.get("order_id"):
                idx[str(o["order_id"])] = ctx
            if o.get("tag"):
                idx["tag:" + str(o["tag"])] = ctx
    return idx


def capture_attempts() -> int:
    have = _keys("attempts.jsonl")
    new = []
    for p in _run_files():
        try:
            run = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        sig = run.get("signal") or {}
        for n, o in enumerate(run.get("orders") or [], 1):
            key = f"{p.stem}#{n}"
            if key in have:
                continue
            new.append({
                "type": "attempt", "key": key, "run_id": p.stem,
                "run_started": run.get("started"), "run_halted": run.get("halted"),
                "signal_date": sig.get("date") or run.get("month_signal_date"),
                "signal": sig.get("signal"), "symbol": o.get("symbol"),
                "trading_symbol": o.get("trading_symbol"), "series": o.get("series"),
                "side": o.get("side"), "qty": o.get("qty"), "decision_mark": o.get("mark"),
                "limit_price": o.get("limit"), "tick_size": o.get("tick_size"),
                "client_tag": o.get("tag"), "status": o.get("status"),
                "broker_order_id": o.get("order_id"), "reject_reason": o.get("reason"),
                "filled_qty": o.get("filled_qty"), "avg_price": o.get("avg_price"),
                "target_qty": o.get("target_qty"), "current_qty": o.get("current_qty"),
                "reached_broker": bool(o.get("order_id")), "account": C.CLIENT_ACCOUNT,
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "source": f"local:{p.name}", "raw": o,
            })
    return _append("attempts.jsonl", new)


# ── broker capture (ours only) ───────────────────────────────────────────────

def capture_orders(kite, orders: list = None) -> int:
    have = _keys("orders.jsonl")
    ctx = decision_index()
    new = []
    for o in orders if orders is not None else K.our_orders(kite):
        oid = str(o.get("order_id") or "")
        st = str(o.get("status") or "").upper()
        key = f"{oid}|{st}|{K._i(o.get('filled_quantity'))}|{K._f(o.get('average_price'))}|{o.get('status_message') or ''}"
        if not oid or key in have:
            continue
        tag = K.order_tag(o)
        d = ctx.get(oid) or ctx.get(f"tag:{tag}") or {}
        new.append({
            "type": "order_state", "key": key, "strategy": "dualmom",
            "account": C.CLIENT_ACCOUNT, "broker_user": o.get("placed_by"),
            "broker_order_id": oid, "exchange_order_id": o.get("exchange_order_id"),
            "client_tag": tag, "symbol": K.base_symbol(o.get("tradingsymbol")),
            "trading_symbol": o.get("tradingsymbol"), "segment": o.get("exchange"),
            "product": o.get("product"), "side": o.get("transaction_type"),
            "price_type": o.get("order_type"), "validity": o.get("validity"),
            "order_qty": K._i(o.get("quantity")), "filled_qty": K._i(o.get("filled_quantity")),
            "unfilled_qty": K._i(o.get("pending_quantity")),
            "cancelled_qty": K._i(o.get("cancelled_quantity")),
            "limit_price": K._f(o.get("price")), "avg_price": K._f(o.get("average_price")),
            "status": st.lower(), "reject_reason": o.get("status_message") if st == "REJECTED" else None,
            "order_entry_time": _iso(o.get("order_timestamp")),
            "exchange_confirm_time": _iso(o.get("exchange_timestamp")),
            "broker_update_time": _iso(o.get("exchange_update_timestamp")),
            **{k: d.get(k) for k in ("run_id", "signal_date", "decision_mark", "target_qty",
                                     "target_weight")},
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source": "kite.orders", "raw": o,
        })
    return _append("orders.jsonl", new)


def capture_fills(kite, orders: list = None) -> int:
    orders = orders if orders is not None else K.our_orders(kite)
    ours = {str(o["order_id"]): o for o in orders}
    if not ours:
        return 0
    have = _keys("fills.jsonl")
    ctx = decision_index()
    new = []
    for t in kite.trades() or []:
        oid = str(t.get("order_id") or "")
        o = ours.get(oid)
        if not o:
            continue                       # not a DualMom order - never recorded
        fid = str(t.get("trade_id") or "")
        key = f"{oid}|{fid}"
        if not fid or key in have:
            continue
        side = str(t.get("transaction_type")).upper()
        qty = K._i(t.get("quantity"))
        price = K._f(t.get("average_price"))
        notional = round(qty * price, 4)
        tag = K.order_tag(o)
        d = ctx.get(oid) or ctx.get(f"tag:{tag}") or {}
        new.append({
            "type": "fill", "key": key, "strategy": "dualmom", "account": C.CLIENT_ACCOUNT,
            "broker_order_id": oid, "exchange_order_id": t.get("exchange_order_id"),
            "exchange_fill_id": fid, "client_tag": tag,
            "symbol": K.base_symbol(t.get("tradingsymbol")), "trading_symbol": t.get("tradingsymbol"),
            "series": "BE" if str(t.get("tradingsymbol", "")).upper().endswith("-BE") else "EQ",
            "segment": t.get("exchange"), "product": t.get("product"), "side": side,
            "qty": qty, "price": price, "notional": notional,
            "exchange_time": _iso(t.get("exchange_timestamp") or t.get("fill_timestamp")),
            "fill_time": _iso(t.get("fill_timestamp")),
            "order_time": _iso(t.get("order_timestamp")),
            "slippage_bps_vs_decision": LK._slippage_bps(side, price, d.get("decision_mark")),
            "charges_est": LK.estimate_charges(side, notional, C.CHARGES_RATE_CARD),
            **{k: d.get(k) for k in ("run_id", "signal_date", "decision_mark", "limit_price",
                                     "target_qty", "target_weight")},
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source": "kite.trades", "raw": t,
        })
    return _append("fills.jsonl", new)


def capture(kite) -> dict:
    with _lock:
        out = {"attempts": capture_attempts()}
        try:
            orders = K.our_orders(kite)
            out["orders"] = capture_orders(kite, orders)
            out["fills"] = capture_fills(kite, orders)
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
        if out.get("orders") or out.get("fills") or out.get("error"):
            event("capture", out)
        return out


# ── own book + own cash ──────────────────────────────────────────────────────

def own_book() -> dict:
    """FIFO positions from OUR fills only (the Kotak helper, same maths)."""
    return LK.own_book(_read("fills.jsonl"))


def own_cash(fills: list = None) -> dict:
    """DualMom's cash inside the shared account, from its own fills:
    capital - buys + sells - estimated charges (incl. DP charge per sell scrip-day)."""
    fills = fills if fills is not None else _read("fills.jsonl")
    buys = sum(f["notional"] for f in fills if f["side"] == "BUY")
    sells = sum(f["notional"] for f in fills if f["side"] == "SELL")
    chg = sum((f.get("charges_est") or {}).get("total", 0.0) for f in fills)
    sell_days = {(f["symbol"], (f.get("exchange_time") or "")[:10]) for f in fills if f["side"] == "SELL"}
    dp = len(sell_days) * C.CHARGES_RATE_CARD.get("dp_per_sell_scrip_day", 0.0)
    cash = C.CAPITAL_BASE - buys + sells - chg - dp
    return {"cash": round(cash, 2), "buys": round(buys, 2), "sells": round(sells, 2),
            "charges_est": round(chg + dp, 2), "dp_est": round(dp, 2)}


def inception_date():
    if C.INCEPTION_DATE:
        return C.INCEPTION_DATE
    ts = sorted(f.get("exchange_time") or "" for f in _read("fills.jsonl") if f.get("exchange_time"))
    return ts[0][:10] if ts else None


# ── NAV series ───────────────────────────────────────────────────────────────

NAV_FIELDS = LK.NAV_FIELDS


def read_nav_daily() -> list:
    p = ROOT / "nav_daily.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_intraday(day: str) -> list:
    p = ROOT / "intraday" / f"{day}.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_row(p: Path, row: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=NAV_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def _dd(nav, prior):
    peak = max([C.CAPITAL_BASE] + [float(x) for x in prior] + [nav])
    return round((nav / peak - 1) * 100, 4) if peak > 0 else 0.0


def record_intraday(snap: dict) -> None:
    prior = [r["nav"] for r in read_nav_daily()] + [r["nav"] for r in read_intraday(snap["date"])]
    _write_row(ROOT / "intraday" / f"{snap['date']}.csv",
               {**snap, "drawdown_pct": _dd(snap["nav"], prior),
                "recorded_at": datetime.now().isoformat(timespec="seconds")})


def record_daily(snap: dict, detail: dict) -> bool:
    with _lock:
        if any(r.get("date") == snap["date"] for r in read_nav_daily()):
            return False
        row = {**snap, "drawdown_pct": _dd(snap["nav"], [r["nav"] for r in read_nav_daily()]),
               "recorded_at": datetime.now().isoformat(timespec="seconds")}
        _write_row(ROOT / "nav_daily.csv", row)
        p = ROOT / "daily" / f"{snap['date']}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({**row, **detail}, indent=2, default=str), encoding="utf-8")
        event("eod_snapshot", {"date": snap["date"], "nav": snap["nav"],
                               "positions": snap.get("positions")})
        return True
