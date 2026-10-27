"""
deployment/stdir_api.py — what the Nifty ST Directional tab reads.
=================================================================

The engine (live_trading_options/nifty_st_directional/engine.py) is a SEPARATE
process. This module only reads the files it publishes: it never imports the
strategy and never writes, so a broken dashboard cannot touch the paper book.

    /api/stdir/tick    ~1 s   spot, bias, open position, live MTM, equity
    /api/stdir/state   ~3 s   spot + option candles, Supertrend, reference low, trades, log
    /api/stdir/mtm     MTM samples for a day (5 s) -> MTM chart
    /api/stdir/daily   per-day summary -> equity curve + days table
    /api/stdir/dates   days that have a chart

Staleness is explicit: a stopped engine stops rewriting its files and the page
says STALE instead of showing a frozen number as live.
"""
import json
import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/stdir", tags=["nifty-st-directional"])

BASE = Path(__file__).resolve().parents[1] / "live_trading_options" / "nifty_st_directional"
STATE = BASE / "data" / "live_state"
RESULTS = BASE / "data" / "results"
PARAMS = BASE / "config" / "parameters.json"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _now() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


@router.get("/tick")
def tick():
    d = _read(STATE / "TICK.json")
    if d is None:
        return {"ok": False, "stale": True, "reason": "engine has not published yet"}
    if d.get("date") != _now().date().isoformat():
        d.update(ok=True, stale=True, age=None, reason=f"last run was {d.get('date')}")
        return d
    try:
        t = dt.datetime.strptime(d.get("ts", ""), "%H:%M:%S")
        n = _now()
        age = (n - n.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)).total_seconds()
    except Exception:
        age = None
    d["age"] = round(age, 1) if age is not None else None
    d["stale"] = age is None or age > 15
    d["ok"] = True
    return d


@router.get("/state")
def state(date: str = Query(None)):
    date = date or _now().date().isoformat()
    d = _read(STATE / f"{date}_STATE.json")
    if d is None:
        return {"ok": False, "date": date, "reason": f"no chart for {date}"}
    try:
        upd = dt.datetime.strptime(d.get("updated", ""), "%Y-%m-%d %H:%M:%S")
        d["age"] = round((_now() - upd).total_seconds(), 1)
    except Exception:
        d["age"] = None
    d["live_day"] = date == _now().date().isoformat()
    d["stale"] = d["live_day"] and (d["age"] is None or d["age"] > 30)
    d["ok"] = True
    return d


@router.get("/mtm")
def mtm(date: str = Query(None)):
    date = date or _now().date().isoformat()
    f = RESULTS / f"{date}_mtm.jsonl"
    rows = []
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return {"ok": True, "date": date, "rows": rows}


STREAM_PORT = 47673          # engine.py StreamHub (127.0.0.1 only)


@router.get("/stream")
async def stream():
    """Server-sent events: every update the engine pushes (spot, forming candles, LTPs,
    paper + Kotak D MTM), ~10 a second while ticks flow. Reconnects to the engine on its
    own; a comment line every 15 s keeps proxies from closing an idle stream."""
    import asyncio
    from fastapi.responses import StreamingResponse

    async def gen():
        yield "retry: 2000\n\n"
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", STREAM_PORT), timeout=3)
            except Exception:
                yield ": engine stream not available\n\n"
                await asyncio.sleep(3)
                continue
            try:
                while True:
                    try:
                        line = await asyncio.wait_for(reader.readline(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if not line:
                        break
                    yield "data: " + line.decode("utf-8", "replace").strip() + "\n\n"
            finally:
                try:
                    writer.close()
                except Exception:
                    pass
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform",
                                      "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@router.get("/daily")
def daily():
    days = _read(RESULTS / "daily.json", {}) or {}
    capital = (_read(PARAMS, {}) or {}).get("capital", 1_000_000)
    rows = sorted(days.values(), key=lambda r: r["date"])
    equity, peak, dd_max, curve = capital, capital, 0.0, []
    for r in rows:
        equity = r.get("equity_close", equity + r.get("net", 0))
        peak = max(peak, equity)
        dd = (equity - peak) / peak * 100 if peak else 0.0
        dd_max = min(dd_max, dd)
        r["drawdown_pct"] = round(dd, 2)
        curve.append({"date": r["date"], "equity": round(equity, 2), "dd": round(dd, 2)})
    net = sum(r.get("net", 0) for r in rows)
    traded = [r for r in rows if r.get("trades", 0) > 0]
    return {"ok": True, "capital": capital, "days": rows[::-1], "curve": curve,
            "summary": {"sessions": len(rows), "days_traded": len(traded),
                        "trades": sum(r.get("trades", 0) for r in rows),
                        "wins": sum(r.get("wins", 0) for r in rows),
                        "net": round(net, 2),
                        "gross": round(sum(r.get("gross", 0) for r in rows), 2),
                        "charges": round(sum(r.get("charges", 0) for r in rows), 2),
                        "return_pct": round(net / capital * 100, 2) if capital else None,
                        "equity": round(capital + net, 2), "max_dd_pct": round(dd_max, 2)}}


@router.get("/dates")
def dates():
    out = sorted({p.name[:10] for p in STATE.glob("????-??-??_STATE.json")}, reverse=True)
    return {"ok": True, "dates": out, "today": _now().date().isoformat()}


@router.get("/compare")
def compare():
    """Paper (live ticks) vs after-market backtest of the same day, per day."""
    paper = _read(RESULTS / "daily.json", {}) or {}
    bt = _read(RESULTS / "backtest_daily.json", {}) or {}
    rows = []
    for d in sorted(set(paper) | set(bt)):
        p, b = paper.get(d), bt.get(d)
        rows.append({
            "date": d,
            "paper_net": p.get("net") if p else None, "paper_gross": p.get("gross") if p else None,
            "paper_trades": p.get("trades") if p else None,
            "bt_net": b.get("net") if b else None, "bt_gross": b.get("gross") if b else None,
            "bt_trades": b.get("trades") if b else None, "bt_detail": b.get("detail", []) if b else [],
            "diff": round(p["net"] - b["net"], 2) if (p and b) else None,
        })
    both = [r for r in rows if r["diff"] is not None]
    return {"ok": True, "rows": rows,
            "summary": {"days": len(both),
                        "paper_net": round(sum(r["paper_net"] for r in both), 2),
                        "bt_net": round(sum(r["bt_net"] for r in both), 2),
                        "diff": round(sum(r["diff"] for r in both), 2)}}


# ── LIVE: Kotak D mirror controls (owner only — dashboard is behind Cloudflare Access) ──
from fastapi import Body                                              # noqa: E402

CONTROL_KD = STATE / "live_control_KOTAK_D.json"
MAX_LOTS = 50


def _kd_read() -> dict:
    d = _read(CONTROL_KD, {}) or {}
    return {"armed": bool(d.get("armed", False)), "lots": int(d.get("lots", 1) or 0),
            "kill": bool(d.get("kill", False)), "updated": d.get("updated")}


def _kd_write(**kw) -> dict:
    d = _kd_read()
    d.update(kw)
    d["updated"] = _now().strftime("%Y-%m-%d %H:%M:%S")
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = CONTROL_KD.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    tmp.replace(CONTROL_KD)
    return d


def _kd_book_view(date: str):
    """A past day's Kotak D book (results/{date}_kotak_d.json) in the same shape the
    engine publishes live. Realized P&L from the day's own fills."""
    b = _read(RESULTS / f"{date}_kotak_d.json")
    if not b:
        return None
    fills = b.get("fills") or []
    norm = []
    for f in fills:
        norm.append({"time": f.get("time") or str(f.get("fill_time") or "")[-8:],
                     "side": f.get("side"), "symbol": f.get("symbol") or f.get("trading_symbol"),
                     "qty": int(f.get("qty") or 0), "price": f.get("price", f.get("avg_price")),
                     "order_id": f.get("order_id"), "limit": f.get("limit"),
                     "price_src": f.get("price_src"), "attempt": f.get("attempt"),
                     "ms_decision_to_send": f.get("ms_decision_to_send"), "ms_ack": f.get("ms_ack"),
                     "ms_send_to_fill_seen": f.get("ms_send_to_fill_seen"), "ms_total": f.get("ms_total")})
    sells = [f for f in norm if str(f["side"]).upper().startswith("S")]
    buys = [f for f in norm if str(f["side"]).upper().startswith("B")]
    # FIFO per contract (averaging all sells against all buys across different
    # contracts was wrong as soon as a day had two trades)
    realized, opens = 0.0, {}
    for f in norm:
        q, px, sym = f["qty"], float(f["price"] or 0), f["symbol"]
        if str(f["side"]).upper().startswith("S"):
            opens.setdefault(sym, []).append([q, px])
            continue
        lots = opens.get(sym, [])
        while q > 0 and lots:
            take = min(q, lots[0][0])
            realized += (lots[0][1] - px) * take
            lots[0][0] -= take
            q -= take
            if lots[0][0] == 0:
                lots.pop(0)
    trades = []
    try:                     # same round-trip builder the engine uses live
        from live_trading_options.nifty_st_directional.live_kotak import round_trips, link_paper
        st = _read(STATE / f"{date}_STATE.json", {}) or {}
        trades = link_paper(round_trips(fills, date), st.get("trades") or [])
    except Exception:
        pass
    return {"trades": trades, "position": b.get("pos"), "fills": norm, "halted": b.get("halted"),
            "last_error": b.get("last_error"), "pending_exit": b.get("pending_exit"),
            "realized_gross": round(realized, 2), "open_gross": 0.0, "day_gross": round(realized, 2),
            "logged_in": None, "n_trades": min(len(sells), len(buys))}


@router.get("/live")
def live_status(date: str = Query(None)):
    today = _now().date().isoformat()
    if date and date != today:
        return {"ok": True, "control": _kd_read(), "engine": _kd_book_view(date), "date": date,
                "history": True}
    t = _read(STATE / "TICK.json", {}) or {}
    eng = t.get("live") if t.get("date") == today else None
    if eng is None or "trades" not in eng:
        # engine not running / older engine: today's trades from Kotak D's own book
        book = _kd_book_view(today)
        if book:
            eng = {**book, **(eng or {}), "trades": book["trades"], "n_trades": book["n_trades"]}
    return {"ok": True, "control": _kd_read(), "engine": eng, "engine_ts": t.get("ts"),
            "engine_date": t.get("date"), "date": today, "history": False}


@router.get("/live/daily")
def live_daily():
    """Live vs paper vs backtest per day, ALL at the lots Kotak D traded that day.

    live      Kotak D's own closed round trips (real fills), net of charges
    paper     every closed paper trade (results/trades.jsonl) re-priced at live's quantity
    backtest  the after-close Breeze re-run (results/backtest_daily.json) at live's quantity
    Charges are recomputed at the scaled quantity (statutory_costs), never divided.
    Lots of a day = what Kotak D traded; on a day it did not trade, the last lots used
    (else the current control). Days start when Kotak D was first set up."""
    from live_trading_options.nifty_st_directional.live_kotak import round_trips
    from backtesting.options_pivot_intraday.costs import statutory_costs
    from backtesting.options_st_directional.config import lot_size

    def leg(sell, buy, qty, d):
        return (sell - buy) * qty - statutory_costs(sell, buy, qty, d)["total"]

    kd = {}
    for f in sorted(RESULTS.glob("????-??-??_kotak_d.json")):
        b = _read(f, {}) or {}
        d = f.name[:10]
        trips = [t for t in round_trips(b.get("fills") or [], d) if not t["open"]] if b.get("fills") else []
        kd[d] = trips
    if not kd:
        return {"ok": True, "rows": [], "summary": {}}
    first = min(kd)

    paper = {}
    pf = RESULTS / "trades.jsonl"
    if pf.exists():
        for line in pf.read_text(encoding="utf-8").splitlines():
            try:
                t = json.loads(line)
            except Exception:
                continue
            if t.get("exit_price") is None or t.get("date", "") < first:
                continue
            paper.setdefault(t["date"], {})[t["trade_id"]] = t          # last write wins
    bt = {d: v for d, v in (_read(RESULTS / "backtest_daily.json", {}) or {}).items() if d >= first}

    days = sorted({d for d in kd if d in bt or d in paper or kd[d]} | set(paper) | set(bt))
    lots_now = _kd_read().get("lots") or 1
    last_lots, rows = None, []
    for d in days:
        trips = kd.get(d) or []
        lots = None
        for t in trips:
            ls = lot_size(t.get("expiry") or d) if t.get("expiry") else None
            if ls:
                lots = max(lots or 0, round(t["qty"] / ls))
        if lots is None and trips:
            lots = max(round(t["qty"] / 65) for t in trips)
        if lots is None:
            lots = last_lots or lots_now
        last_lots = lots
        live_net = round(sum(t["net"] or 0 for t in trips), 2)
        p_net = 0.0
        for t in (paper.get(d) or {}).values():
            q = lot_size(str(t["expiry"])[:10]) * lots
            p_net += leg(float(t["entry_price"]), float(t["exit_price"]), q, d)
        b_net, b_n = None, 0
        if d in bt:
            b_net = 0.0
            for t in bt[d].get("detail") or []:
                q = lot_size(str(t["expiry"])[:10]) * lots
                b_net += leg(float(t["entry_price"]), float(t["exit_price"]), q, d)
                b_n += 1
        rows.append({"date": d, "lots": lots, "live_net": live_net, "live_trades": len(trips),
                     "live_wins": sum(1 for t in trips if (t["net"] or 0) > 0),
                     "live_charges": round(sum(t["charges"] or 0 for t in trips), 2),
                     "paper_net": round(p_net, 2), "paper_trades": len(paper.get(d) or {}),
                     "bt_net": None if b_net is None else round(b_net, 2), "bt_trades": b_n,
                     "cpr_skipped": bool(bt.get(d, {}).get("cpr_skipped"))})
    summ = {"days": len(rows)}
    for k in ("live", "paper", "bt"):
        cum, peak, mdd = 0.0, 0.0, 0.0
        for r in rows:
            v = r[f"{k}_net"]
            if v is not None:
                cum += v
            peak = max(peak, cum)
            r[f"{k}_cum"], r[f"{k}_dd"] = round(cum, 2), round(cum - peak, 2)
            mdd = min(mdd, cum - peak)
        summ[f"{k}_net"], summ[f"{k}_max_dd"] = round(cum, 2), round(mdd, 2)
    summ["live_trades"] = sum(r["live_trades"] for r in rows)
    summ["live_wins"] = sum(r["live_wins"] for r in rows)
    summ["live_charges"] = round(sum(r["live_charges"] for r in rows), 2)
    return {"ok": True, "rows": rows, "summary": summ}


@router.get("/live/dates")
def live_dates():
    """Days on which Kotak D actually traded (have fills), newest first, plus today."""
    out = []
    for f in RESULTS.glob("????-??-??_kotak_d.json"):
        b = _read(f, {}) or {}
        if b.get("fills"):
            out.append(f.name[:10])
    today = _now().date().isoformat()
    return {"ok": True, "dates": sorted(set(out) | {today}, reverse=True), "today": today}


@router.post("/live/control")
def live_control(body: dict = Body(...)):
    """{"armed": true|false, "lots": n} — either key optional. Lots apply to the NEXT entry."""
    kw = {}
    if "lots" in body:
        try:
            lots = int(body["lots"])
        except Exception:
            return {"ok": False, "error": "lots must be a whole number"}
        if not 0 <= lots <= MAX_LOTS:
            return {"ok": False, "error": f"lots must be 0..{MAX_LOTS}"}
        kw["lots"] = lots
    if "armed" in body:
        kw["armed"] = bool(body["armed"])
    if not kw:
        return {"ok": False, "error": "nothing to change"}
    return {"ok": True, "control": _kd_write(**kw)}


@router.post("/live/kill")
def live_kill():
    """Buy back anything open on Kotak D now, disarm, no more entries today."""
    return {"ok": True, "control": _kd_write(kill=True, armed=False)}
