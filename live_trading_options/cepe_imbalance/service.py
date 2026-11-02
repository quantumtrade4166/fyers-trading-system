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
  GET  /history/{name}?date=         MTM + imbalance samples (1 s)
  POST /control/{name}  {"action": "start"|"stop"|"stop_exit"|"exit_all", "expiry_n": 0}
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
from live_trading_options.cepe_imbalance import instruments
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
def chain(name: str, n: int = 10, expiry_n: int = 0):
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    e = E.market.expiry(nm, expiry_n)
    return {"name": nm, "expiry": e, "expiries": E.u.underlyings[nm].expiries()[:4],
            "ref": E.market.ref_price(nm, e), "atm": E.market.atm(nm, e),
            "rows": E.market.chain_rows(nm, e, max(1, min(n, 30))) if e else []}


@app.get("/positions/{name}")
def positions(name: str):
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    r = E.runners[nm]
    return {"name": nm, "positions": r.book.open_positions(), "mtm": r.book.mtm(), "imbalance": r.book.imbalance()}


@app.get("/trades/{name}")
def trades(name: str):
    nm = _name(name)
    return {"name": nm, "trades": E.runners[nm].book.trades[-500:]} if nm else _bad("unknown underlying", 404)


@app.get("/events/{name}")
def events(name: str):
    nm = _name(name)
    return {"name": nm, "events": E.runners[nm].events[-200:]} if nm else _bad("unknown underlying", 404)


@app.get("/history/{name}")
def history(name: str, date: str = ""):
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    date = date or dt.datetime.now(IST).date().isoformat()
    p = C.DATA / "history" / date / f"{nm}.jsonl"
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []
    return {"name": nm, "date": date, "rows": rows}


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
    log.info("CE/PE imbalance paper engine up on %s:%s", C.HOST, C.PORT)
    uvicorn.run(app, host=C.HOST, port=C.PORT, log_level="warning")


if __name__ == "__main__":
    main()
