"""
Client ledger for Kotak Rohit (UCC 15P56) - the permanent record of every trade.

WHAT IS RECORDED
    fills.jsonl     one line per EXCHANGE FILL (Kotak trade_report). An order for
                    207 IFCI can execute in many pieces; each piece is its own line
                    with exchange fill id, exchange order id, exchange timestamp,
                    qty and price. This is the ground truth of what was bought.
    orders.jsonl    one line per broker ORDER STATE (Kotak order_report): placed,
                    partially filled, complete, rejected, cancelled - every change.
    attempts.jsonl  one line per order WE TRIED to send, from our own run files -
                    including orders refused before they ever reached the broker
                    (all 40 at 11:01 on 2026-09-15 never appear in order_report).
    events.jsonl    lifecycle: deploys, rebalances, stops, reconciliation breaks,
                    captures, corrections.
    daily/YYYY-MM-DD.json   end-of-day snapshot of the whole book.
    nav_daily.csv   one row per session: NAV, cash, market value, P&L, drawdown.
    intraday/YYYY-MM-DD.csv NAV every few minutes while the market is open.

INTEGRITY
    Each .jsonl file is an APPEND-ONLY HASH CHAIN: every record carries the SHA-256
    of the previous record and its own. Editing or deleting any line breaks the
    chain from that point on, and verify() says exactly where. Nothing in this
    module ever rewrites a line; a correction is a NEW record that references the
    old one.

    Every broker record keeps the complete raw payload next to the normalised
    fields, so a field we did not think to extract today is still recoverable.

IDEMPOTENT
    capture() can run every few minutes. Each record has a natural key (fill id,
    order state, attempt id) and a key already present is never written twice.

Nothing here places, modifies or cancels an order.
"""

import csv
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

from deployment.dualmom_live import config as C

ROOT = Path(__file__).resolve().parents[1] / C.STATE_DIR / "ledger"
_lock = threading.RLock()

TAG_PREFIX = C.ORDER_TAG
GOLIVE_TEST_TAG = "dualmom"        # the 1-share IDEA go-live test on 2026-09-15


# ── hash-chained append-only files ───────────────────────────────────────────

def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str,
                      ensure_ascii=False)


def _path(name: str) -> Path:
    return ROOT / name


def _read(name: str) -> list:
    p = _path(name)
    if not p.exists():
        return []
    out = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _append(name: str, records: list) -> int:
    """Append records to a hash chain. Returns how many were written."""
    if not records:
        return 0
    with _lock:
        ROOT.mkdir(parents=True, exist_ok=True)
        existing = _read(name)
        prev = existing[-1]["hash"] if existing else "GENESIS"
        seq = existing[-1]["seq"] if existing else 0
        with _path(name).open("a", encoding="utf-8", newline="\n") as f:
            for rec in records:
                seq += 1
                body = {**rec, "seq": seq, "prev_hash": prev}
                h = hashlib.sha256((prev + _canonical(body)).encode("utf-8")).hexdigest()
                body["hash"] = h
                f.write(_canonical(body) + "\n")
                prev = h
        return len(records)


def verify(name: str) -> dict:
    """Recompute a chain. Returns {ok, records, broken_at_seq, reason}."""
    prev = "GENESIS"
    recs = _read(name)
    for i, rec in enumerate(recs, 1):
        h = rec.get("hash")
        body = {k: v for k, v in rec.items() if k != "hash"}
        if rec.get("prev_hash") != prev:
            return {"ok": False, "records": len(recs), "broken_at_seq": i,
                    "reason": "prev_hash does not match the previous record"}
        if rec.get("seq") != i:
            return {"ok": False, "records": len(recs), "broken_at_seq": i,
                    "reason": "sequence gap - a record was removed"}
        if hashlib.sha256((prev + _canonical(body)).encode("utf-8")).hexdigest() != h:
            return {"ok": False, "records": len(recs), "broken_at_seq": i,
                    "reason": "record content was altered"}
        prev = h
    return {"ok": True, "records": len(recs), "broken_at_seq": None, "reason": None}


def verify_all() -> dict:
    return {n: verify(n) for n in ("fills.jsonl", "orders.jsonl", "attempts.jsonl",
                                   "events.jsonl")}


# ── normalisation helpers ────────────────────────────────────────────────────

def _f(x, default=0.0):
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _i(x, default=0):
    try:
        return int(float(str(x).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def _kotak_time(s):
    """'15-Sep-2026 11:13:36' or '2026/09/15 11:13:36' -> ISO, else None."""
    s = str(s or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None


def attribute(tag) -> str:
    """Which part of the system placed an order, from its client tag."""
    t = str(tag or "")
    if t.startswith(TAG_PREFIX) and t[len(TAG_PREFIX):].isdigit():
        return "dualmom"
    if t == GOLIVE_TEST_TAG:
        return "dualmom_golive_test"
    return "unattributed"          # e.g. a manual trade in the Kotak app - flag it


def estimate_charges(side: str, notional: float, rate_card: dict = None) -> dict:
    """Statutory + broker charges for one NSE delivery fill. ESTIMATE."""
    rc = rate_card or C.CHARGES_RATE_CARD
    buy = side == "BUY"
    brokerage = notional * rc["brokerage_pct"]
    stt = notional * (rc["stt_buy"] if buy else rc["stt_sell"])
    stamp = notional * rc["stamp_buy"] if buy else 0.0
    txn = notional * rc["exch_txn"]
    sebi = notional * rc["sebi"]
    gst = (brokerage + txn + sebi) * rc["gst"]
    total = brokerage + stt + stamp + txn + sebi + gst
    return {"brokerage": round(brokerage, 4), "stt": round(stt, 4),
            "stamp": round(stamp, 4), "exch_txn": round(txn, 4),
            "sebi": round(sebi, 4), "gst": round(gst, 4), "total": round(total, 4),
            "rate_card": rc["version"], "estimate": True,
            "brokerage_verified": rc.get("brokerage_verified", False)}


def _slippage_bps(side, price, mark):
    """Positive = cost. BUY above the decision mark, or SELL below it."""
    if not mark or mark <= 0 or not price:
        return None
    raw = (price - mark) / mark * 1e4
    return round(raw if side == "BUY" else -raw, 2)


# ── decision context from our own run files ──────────────────────────────────

def _runs_dir() -> Path:
    return Path(__file__).resolve().parents[1] / C.STATE_DIR


def _run_files():
    return sorted(_runs_dir().glob("run_*.json")) + sorted(_runs_dir().glob("stop_*.json"))


def decision_index() -> dict:
    """order_id / tag -> the plan row that produced it (mark, limit, target...)."""
    idx = {}
    for p in _run_files():
        try:
            run = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        sig = run.get("signal") or {}
        weights = {h.get("symbol"): h.get("weight") for h in sig.get("holdings", [])}
        for o in run.get("orders") or []:
            ctx = {"run_id": p.stem, "run_started": run.get("started"),
                   "signal_date": sig.get("date") or run.get("month_signal_date"),
                   "decision_mark": o.get("mark"), "limit_price": o.get("limit"),
                   "target_qty": o.get("target_qty"), "current_qty": o.get("current_qty"),
                   "target_weight": weights.get(o.get("symbol")),
                   "reason": o.get("reason")}
            if o.get("order_id"):
                idx[str(o["order_id"])] = ctx
            if o.get("tag"):
                idx["tag:" + str(o["tag"])] = ctx
    return idx


# ── capture ──────────────────────────────────────────────────────────────────

def _rows(resp):
    if isinstance(resp, dict):
        return resp.get("data") or []
    return resp or []


def _keys(name: str, field: str = "key") -> set:
    return {r.get(field) for r in _read(name)}


def capture_attempts() -> int:
    """Every order we tried to send, from run files (incl. pre-broker refusals)."""
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
                "signal_date": sig.get("date"), "signal": sig.get("signal"),
                "symbol": o.get("symbol"), "trading_symbol": o.get("trading_symbol"),
                "series": o.get("series"), "side": o.get("side"), "qty": o.get("qty"),
                "decision_mark": o.get("mark"), "limit_price": o.get("limit"),
                "tick_size": o.get("tick_size"), "client_tag": o.get("tag"),
                "status": o.get("status"), "broker_order_id": o.get("order_id"),
                "reject_reason": o.get("reason"), "filled_qty": o.get("filled_qty"),
                "avg_price": o.get("avg_price"), "target_qty": o.get("target_qty"),
                "current_qty": o.get("current_qty"), "plan_reason": o.get("reason"),
                "reached_broker": bool(o.get("order_id")),
                "account": C.CLIENT_UCC, "captured_at": datetime.now().isoformat(timespec="seconds"),
                "source": f"local:{p.name}", "raw": o,
            })
    return _append("attempts.jsonl", new)


def capture_orders(client) -> int:
    """Every distinct broker order state (order_report)."""
    have = _keys("orders.jsonl")
    ctx = decision_index()
    new = []
    for r in _rows(client.order_report()):
        oid = str(r.get("nOrdNo") or "")
        state = (str(r.get("ordSt") or r.get("stat") or "").lower(), _i(r.get("fldQty")),
                 _f(r.get("avgPrc")), str(r.get("rejRsn") or ""))
        key = f"{oid}|{state[0]}|{state[1]}|{state[2]}|{state[3]}"
        if not oid or key in have:
            continue
        tag = r.get("GuiOrdId")
        side = "BUY" if str(r.get("trnsTp")).upper().startswith("B") else "SELL"
        d = ctx.get(oid) or ctx.get(f"tag:{tag}") or {}
        new.append({
            "type": "order_state", "key": key, "strategy": attribute(tag),
            "account": r.get("actId"), "broker_user": r.get("usrId"),
            "broker_order_id": oid, "exchange_order_id": r.get("exOrdId"),
            "client_tag": tag, "symbol": r.get("sym"), "trading_symbol": r.get("trdSym"),
            "series": r.get("series"), "segment": r.get("exSeg"), "product": r.get("prod"),
            "side": side, "price_type": r.get("prcTp"), "validity": r.get("vldt"),
            "order_qty": _i(r.get("qty")), "filled_qty": _i(r.get("fldQty")),
            "unfilled_qty": _i(r.get("unFldSz")), "cancelled_qty": _i(r.get("cnlQty")),
            "limit_price": _f(r.get("prc")), "avg_price": _f(r.get("avgPrc")),
            "tick_size": _f(r.get("tckSz")), "status": state[0],
            "reject_reason": None if state[3] in ("", "--", "NA") else state[3],
            "order_entry_time": _kotak_time(r.get("ordEntTm")),
            "exchange_confirm_time": _kotak_time(r.get("exCfmTm")),
            "broker_update_time": _kotak_time(r.get("hsUpTm")),
            "order_source": r.get("ordSrc"), **{k: d.get(k) for k in (
                "run_id", "signal_date", "decision_mark", "target_qty", "target_weight")},
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source": "kotak.order_report", "raw": r,
        })
    return _append("orders.jsonl", new)


def capture_fills(client) -> int:
    """Every exchange fill (trade_report). The ground truth of positions."""
    have = _keys("fills.jsonl")
    ctx = decision_index()
    new = []
    for r in _rows(client.trade_report()):
        oid = str(r.get("nOrdNo") or "")
        fid = str(r.get("flId") or "")
        leg = str(r.get("flLeg") or "")
        key = f"{oid}|{fid}|{leg}|{r.get('exTm')}"
        if not oid or not fid or key in have:
            continue
        tag = r.get("GuiOrdId")
        side = "BUY" if str(r.get("trnsTp")).upper().startswith("B") else "SELL"
        qty = _i(r.get("fldQty"))
        price = _f(r.get("avgPrc"))
        notional = round(qty * price, 4)
        d = ctx.get(oid) or ctx.get(f"tag:{tag}") or {}
        new.append({
            "type": "fill", "key": key, "strategy": attribute(tag),
            "account": r.get("actId"), "broker_user": r.get("usrId"),
            "broker_order_id": oid, "exchange_order_id": r.get("exOrdId"),
            "exchange_fill_id": fid, "fill_leg": r.get("flLeg"), "client_tag": tag,
            "symbol": r.get("sym"), "trading_symbol": r.get("trdSym"),
            "series": r.get("series"), "segment": r.get("exSeg"), "product": r.get("prod"),
            "instrument": r.get("it"), "side": side, "qty": qty, "price": price,
            "notional": notional,
            "exchange_time": _kotak_time(r.get("exTm")),
            "fill_date": _kotak_time(r.get("flDt")),
            "broker_update_time": _kotak_time(r.get("hsUpTm")),
            "exchange_epoch": r.get("boeSec"),
            "slippage_bps_vs_decision": _slippage_bps(side, price, d.get("decision_mark")),
            "charges_est": estimate_charges(side, notional),
            **{k: d.get(k) for k in ("run_id", "signal_date", "decision_mark", "limit_price",
                                     "target_qty", "target_weight")},
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source": "kotak.trade_report", "raw": r,
        })
    return _append("fills.jsonl", new)


def event(kind: str, detail: dict) -> None:
    _append("events.jsonl", [{"type": "event", "key": f"{kind}|{datetime.now().isoformat()}",
                              "kind": kind, "at": datetime.now().isoformat(timespec="seconds"),
                              "account": C.CLIENT_UCC, **detail}])


def capture(client) -> dict:
    """All captures, idempotent. Safe to call as often as needed."""
    with _lock:
        out = {"attempts": capture_attempts()}
        try:
            out["orders"] = capture_orders(client)
        except Exception as e:
            out["orders_error"] = f"{type(e).__name__}: {e}"
        try:
            out["fills"] = capture_fills(client)
        except Exception as e:
            out["fills_error"] = f"{type(e).__name__}: {e}"
        if any(out.get(k) for k in ("orders", "fills")) or any(k.endswith("_error") for k in out):
            event("capture", out)
        return out


# ── own book from fills (FIFO) ───────────────────────────────────────────────

def own_book(fills: list = None) -> dict:
    """Positions rebuilt ONLY from exchange fills, FIFO lot matching.

    FIFO because that is how Indian capital-gains are computed on delivery trades,
    so realised P&L here matches the client's tax statement, not an average-cost
    approximation. Returns {symbol: {...}} for open positions plus a 'realized'
    total and the lot detail for every symbol ever traded.
    """
    fills = fills if fills is not None else _read("fills.jsonl")
    fills = sorted(fills, key=lambda x: (x.get("exchange_time") or "", str(x.get("exchange_fill_id"))))
    lots, realized, charges, meta = {}, {}, {}, {}
    for fl in fills:
        s = fl["symbol"]
        meta.setdefault(s, {"trading_symbol": fl.get("trading_symbol"),
                            "series": fl.get("series"), "first_fill": fl.get("exchange_time")})
        meta[s]["last_fill"] = fl.get("exchange_time")
        charges[s] = charges.get(s, 0.0) + (fl.get("charges_est") or {}).get("total", 0.0)
        q, px = int(fl["qty"]), float(fl["price"])
        book = lots.setdefault(s, [])
        if fl["side"] == "BUY":
            book.append({"qty": q, "price": px, "time": fl.get("exchange_time"),
                         "fill_id": fl.get("exchange_fill_id")})
        else:
            rem = q
            while rem > 0 and book:
                lot = book[0]
                take = min(rem, lot["qty"])
                realized[s] = realized.get(s, 0.0) + take * (px - lot["price"])
                lot["qty"] -= take
                rem -= take
                if lot["qty"] == 0:
                    book.pop(0)
            if rem > 0:
                meta[s]["oversold_qty"] = meta[s].get("oversold_qty", 0) + rem
    positions = {}
    for s, book in lots.items():
        qty = sum(l["qty"] for l in book)
        if qty <= 0:
            continue
        cost = sum(l["qty"] * l["price"] for l in book)
        positions[s] = {"qty": qty, "cost": round(cost, 4), "avg_price": round(cost / qty, 4),
                        "lots": len(book), "charges_est": round(charges.get(s, 0.0), 4),
                        **meta.get(s, {})}
    return {"positions": positions,
            "realized": round(sum(realized.values()), 4),
            "realized_by_symbol": {k: round(v, 4) for k, v in realized.items()},
            "charges_est_total": round(sum(charges.values()), 4),
            "fills": len(fills)}


# ── NAV series ───────────────────────────────────────────────────────────────

NAV_FIELDS = ["date", "time", "nav", "market_value", "cash", "cost_basis", "unrealized",
              "realized_cum", "charges_est_cum", "deployed_pct", "positions",
              "benchmark_close", "return_pct", "drawdown_pct", "recorded_at"]


def read_nav_daily() -> list:
    p = _path("nav_daily.csv")
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv_row(p: Path, row: dict):
    ROOT.mkdir(parents=True, exist_ok=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=NAV_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def _drawdown(nav: float, series_navs: list) -> float:
    peak = max([C.CAPITAL_BASE] + [float(x) for x in series_navs] + [nav])
    return round((nav / peak - 1) * 100, 4) if peak > 0 else 0.0


def record_intraday(snapshot: dict) -> None:
    day = snapshot["date"]
    p = _path(f"intraday/{day}.csv")
    prior = []
    if p.exists():
        with p.open(encoding="utf-8", newline="") as f:
            prior = [r["nav"] for r in csv.DictReader(f)]
    daily = [r["nav"] for r in read_nav_daily()]
    row = {**snapshot, "drawdown_pct": _drawdown(snapshot["nav"], daily + prior),
           "recorded_at": datetime.now().isoformat(timespec="seconds")}
    _write_csv_row(p, row)


def read_intraday(day: str) -> list:
    p = _path(f"intraday/{day}.csv")
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def record_daily(snapshot: dict, detail: dict) -> bool:
    """One row per session in nav_daily.csv + the full JSON snapshot. Idempotent per date."""
    day = snapshot["date"]
    with _lock:
        if any(r.get("date") == day for r in read_nav_daily()):
            return False
        daily = [r["nav"] for r in read_nav_daily()]
        row = {**snapshot, "drawdown_pct": _drawdown(snapshot["nav"], daily),
               "recorded_at": datetime.now().isoformat(timespec="seconds")}
        _write_csv_row(_path("nav_daily.csv"), row)
        p = _path(f"daily/{day}.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({**row, **detail}, indent=2, default=str), encoding="utf-8")
        event("eod_snapshot", {"date": day, "nav": snapshot["nav"],
                               "positions": snapshot.get("positions")})
        return True
