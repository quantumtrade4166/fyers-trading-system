"""
cepe_imbalance/service.py — the CE/PE imbalance PAPER engine's own process (127.0.0.1:47690).
=============================================================================================

Start:  .venv\\Scripts\\python.exe -m live_trading_options.cepe_imbalance.service
The port bind is the single-instance lock. Paper only: no order ever reaches a broker.

API (dashboard will proxy /api/cepe/* here):
  GET  /health                       feed + engine status
  GET  /state                        one row per underlying (ref, ATM, MTM, imbalance, session)
  GET  /chain/{name}?n=10&expiry_n=0 live chain around ATM
  GET  /positions/{name}             paper positions
  GET  /trades/{name}                today's paper fills
  GET  /events/{name}                engine/strategy events
  GET  /history/{name}?date=&max_points=  MTM + imbalance samples (0.5 s; downsampled on request)
  GET  /days                         saved trading days
  GET  /day/{date}                   EOD P&L of every instrument that day (trades/events/positions take ?date= too)
  POST /control/{name}  {"action": "start"|"stop"|"stop_exit"|"exit_all", "expiry_n": 0}
  POST /params/{name}   {"lots":1,"band":15,"normal_band":5,"atm_buffer_pct":20,"mtm_stop":0,"step":0}
  POST /reset/{name}    {"start": true}   archive today's paper book (data/books/{date}/_reset_*) + restart
  POST /auto/{name}     {"auto_start": true, "auto_time": "09:05"}   daily auto-start (saved)
  POST /order/{name}    {"strike":..., "kind":"CE"|"PE", "side":"BUY"|"SELL", "lots":1}   manual paper order
"""
import datetime as dt
import json
import logging
import socket
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import days, instruments
from live_trading_options.cepe_imbalance.book import IST
from live_trading_options.cepe_imbalance.engine import Engine
from live_trading_options.cepe_imbalance.feed import MultiFeed
from live_trading_options.cepe_imbalance.market import Market

log = logging.getLogger("cepe.service")
app = FastAPI(title="CE/PE Imbalance (paper)")
E: Engine | None = None


def _bad(msg, code=400):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


def _name(name):
    name = name.upper()
    return name if E and name in E.runners else None


@app.get("/health")
def health():
    if not E:
        return {"ok": False, "error": "starting"}
    f = E.feed.status() if E.feed else {}
    return {"ok": bool(f.get("authed")), "feed": f, "universe_date": E.u.file_date}


@app.get("/state")
def state():
    return E.summary()


@app.get("/chain/{name}")
def chain(name: str, n: int = 10, expiry_n: int = 0, grid: int = 1):
    """Live chain around ATM. grid=1 keeps only the strategy's strike grid; rows carry our held lots."""
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    r = E.runners[nm]
    e = E.market.expiry(nm, expiry_n)
    n = max(1, min(n, 30))
    step = float(r.params.get("step") or C.STRIKE_STEP.get(nm) or 0)
    rows = []
    if e:
        listed = E.u.underlyings[nm].strikes(e)
        gap = min((b - a for a, b in zip(listed, listed[1:])), default=step) or step
        ratio = max(1, int(round(step / gap))) if grid and step else 1
        center = r.state.get("atm") or E.market.atm(nm, e)
        rows = E.market.chain_rows(nm, e, n * ratio)
        if ratio > 1:
            rows = [x for x in rows if abs(x["strike"] / step - round(x["strike"] / step)) < 1e-9]
        held = {}
        for p in r.book.positions.values():
            if p.lots and p.expiry == e:
                held[(p.strike, p.kind)] = p.lots
        for x in rows:
            x["atm"] = center is not None and x["strike"] == center
            for kind in ("CE", "PE"):
                x[kind]["held"] = held.get((x["strike"], kind), 0)
    return {"name": nm, "expiry": e, "expiries": E.u.underlyings[nm].expiries()[:4], "step": step,
            "ref": E.market.ref_price(nm, e), "atm": r.state.get("atm") or E.market.atm(nm, e), "rows": rows}


def _is_today(nm, date):
    return not date or date == E.runners[nm].date


@app.get("/positions/{name}")
def positions(name: str, date: str = ""):
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    r = E.runners[nm]
    if _is_today(nm, date):
        return {"name": nm, "positions": r.book.open_positions(), "mtm": r.book.mtm(), "imbalance": r.book.imbalance()}
    rows = [{**p, "ltp": None, "unrealized": 0.0} for p in days.positions(date, nm)]
    return {"name": nm, "date": date, "positions": rows}


@app.get("/trades/{name}")
def trades(name: str, date: str = ""):
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    rows = E.runners[nm].book.trades if _is_today(nm, date) else days.trades(date, nm)
    return {"name": nm, "trades": rows[-1000:]}


@app.get("/events/{name}")
def events(name: str, date: str = ""):
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    r = E.runners[nm]
    if _is_today(nm, date):                               # file survives engine restarts; memory does not
        rows = days.events(r.date, nm) if E.persist else r.events
    else:
        rows = days.events(date, nm)
    return {"name": nm, "events": rows[-500:]}


@app.get("/history/{name}")
def history(name: str, date: str = "", since: float = 0, max_points: int = 0):
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    date = date or E.runners[nm].date
    rows = list(E.hist.get(nm, [])) if date == E.runners[nm].date else days.history(date, nm)
    if since:
        rows = [x for x in rows if x.get("t", 0) > since]
    if max_points > 0:
        rows = days.downsample(rows, max_points)
    return {"name": nm, "date": date, "rows": rows}


@app.get("/days")
def day_list():
    today = next(iter(E.runners.values())).date
    return {"today": today, "days": sorted(set(days.list_days()) | {today}, reverse=True)}


@app.get("/day/{date}")
def day(date: str):
    """EOD P&L table for every instrument on one day (today = live numbers)."""
    rows = []
    for nm, r in E.runners.items():
        s = days.summary(date, nm, hist=list(E.hist.get(nm, [])) if date == r.date else None)
        if date == r.date:                                 # today: live MTM, not the last file sample
            m = r.book.mtm()
            s.update(pnl=m["total"], realized=m["realized"], open_lots=sum(abs(p.lots) for p in r.book.positions.values()),
                     fills=len(r.book.trades), traded=bool(r.book.trades), running=r.running, phase=r.state.get("phase"))
        rows.append(s)
    return {"date": date, "rows": rows, "total": round(sum(x["pnl"] for x in rows), 2)}


@app.post("/control/{name}")
def control(name: str, body: dict = Body(...)):
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    a = str(body.get("action", ""))
    if "expiry_n" in body:
        E.runners[nm].expiry_n = max(0, min(int(body["expiry_n"]), 3))
    if a == "start":
        return E.start(nm)
    if a == "stop":
        return E.stop(nm)
    if a == "stop_exit":
        return E.stop(nm, exit_positions=True)
    if a == "exit_all":
        with E.lock:
            return {"ok": True, "exits": E.runners[nm].book.exit_all("manual exit all")}
    return _bad(f"unknown action {a!r}")


@app.post("/params/{name}")
def params(name: str, body: dict = Body(...)):
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    try:
        return E.set_params(nm, body)
    except (TypeError, ValueError) as e:
        return _bad(f"bad params: {e}")


@app.post("/reset/{name}")
def reset(name: str, body: dict = Body(default={})):
    """Archive today's paper book for this instrument and start fresh ({"start": false} to only wipe)."""
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    return E.reset(nm, start=bool((body or {}).get("start", True)))


@app.post("/auto/{name}")
def auto(name: str, body: dict = Body(...)):
    """{"auto_start": true|false, "auto_time": "09:05"} — saved, applies every day."""
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    try:
        return E.set_auto(nm, body.get("auto_start"), body.get("auto_time"))
    except (TypeError, ValueError) as e:
        return _bad(f"bad auto-start setting: {e}")


@app.post("/order/{name}")
def order(name: str, body: dict = Body(...)):
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    try:
        return E.manual_order(nm, float(body["strike"]), str(body["kind"]), str(body["side"]),
                              int(body.get("lots", 1)), body.get("expiry_n"))
    except (KeyError, ValueError) as e:
        return _bad(f"bad order: {e}")


def main():
    import uvicorn
    from live_trading_options.mcx_terminal import kotak_session
    from live_trading_options.mcx_terminal import config as MC

    C.DATA.mkdir(parents=True, exist_ok=True)
    C.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(C.LOG_DIR / "cepe_imbalance.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    # single instance: refuse if the port is taken
    s = socket.socket()
    try:
        s.bind((C.HOST, C.PORT))
    except OSError:
        log.error("port %s busy: another CE/PE imbalance engine is running", C.PORT)
        sys.exit(1)
    s.close()

    MC.ENV_SUFFIX = C.ENV_SUFFIX                          # Kotak P login (same account as the MCX terminal)
    sessions = kotak_session.SessionHolder()
    sess = sessions.get()
    u = instruments.load(sess.client)
    market = Market(u)
    global E
    E = Engine(u, market)
    feed = MultiFeed(sessions, E.on_ticks)
    E.feed = feed
    E.refresh_subscriptions()                             # spots + futures first; windows once ATM is known
    feed.start()
    E.run_timer()

    def daily_exit():                                     # 08:45 IST next day -> exit 3; the task starts a
        import os, time as _t                             # fresh engine (new Kotak login + contract list)
        born = dt.datetime.now(IST).date()
        while True:
            _t.sleep(30)
            now = dt.datetime.now(IST)
            if now.date() > born and now.strftime("%H:%M") >= "08:45":
                log.info("daily restart (exit 3)")
                os._exit(3)
    import threading
    threading.Thread(target=daily_exit, daemon=True, name="CepeDailyExit").start()
    log.info("CE/PE imbalance paper engine up on %s:%s", C.HOST, C.PORT)
    uvicorn.run(app, host=C.HOST, port=C.PORT, log_level="warning")


if __name__ == "__main__":
    main()
