"""
strategy_builder/service.py — the Strategy Builder's own process (port 47674).
==========================================================================

The dashboard's "Strategy Builder" tab is a page (deployment/static/builder.html)
whose /api/sb/* calls the dashboard forwards here. Running as its own process means
the builder can be restarted at any time, mid-market, without touching the dashboard
or any live engine — and a crash here cannot reach them.

STAGE 1 (this file): live chain, saved strategies. NO ORDER CAN BE PLACED from here.

Run:
    .venv\\Scripts\\python.exe -m live_trading_options.strategy_builder.service
"""
import sys
import logging

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from fastapi import FastAPI, Body, Query
from fastapi.responses import JSONResponse

from live_trading_options.strategy_builder import config as C
from live_trading_options.strategy_builder import store
from live_trading_options.strategy_builder.chain_feed import ChainFeed

C.LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout),
                              logging.FileHandler(C.LOG_DIR / "strategy_builder.log", encoding="utf-8")])

app = FastAPI(title="Strategy Builder")
feed = ChainFeed()
hub = None
desk = None


def _leg_quotes(legs):
    """Live LTP for deployment legs (keeps their chains watched while positions are open)."""
    for l in legs:
        if l.get("index") and l.get("expiry"):
            feed.touch(l["index"], l["expiry"])
    return feed.quotes([l["fyers_symbol"] for l in legs if l.get("fyers_symbol")])


@app.on_event("startup")
def _start():
    global hub, desk
    feed.start()
    # brokers / order desk load lazily-safe: a broker import problem must not take the chain down
    try:
        from live_trading_options.strategy_builder import brokers as B
        from live_trading_options.strategy_builder import orders as O
        hub = B.BrokerHub()
        desk = O.Desk(hub, _leg_quotes, {"kite": O.KiteAdapter(B.kx), "kotak": O.KotakAdapter(B.ke)},
                      B.resolve)
        hub.start()
        desk.run_monitor()
    except Exception as e:
        logging.getLogger("sb").exception("order desk NOT started: %s", e)


def _bad(msg, code=400):
    return JSONResponse(status_code=code, content={"ok": False, "error": msg})


PAGE = C.ROOT / "deployment" / "static" / "builder.html"


@app.get("/")
def page():
    """The same page the dashboard tab shows — handy for opening the builder directly
    on the VPS (http://127.0.0.1:47674/) when the dashboard itself is being restarted."""
    from fastapi.responses import FileResponse
    return FileResponse(PAGE, headers={"Cache-Control": "no-store"})


@app.get("/api/sb/health")
def health():
    return {"ok": True, "feed": feed.status()}


@app.get("/api/sb/meta")
def meta():
    return {"ok": True, "indices": list(C.INDICES), "lot": C.LOT_SIZE, "step": C.STRIKE_STEP,
            "expiries": feed.expiries, "under": {i: feed.underlying(i) for i in C.INDICES},
            "feed": feed.status()}


@app.get("/api/sb/chain")
def chain(index: str = Query(...), expiry: str = Query(None)):
    if index not in C.INDICES:
        return _bad(f"unknown index {index}")
    if not expiry:
        ex = feed.expiries.get(index)
        if not ex:
            return {"ok": False, "loading": True, "index": index}
        expiry = ex[0]["date"]
    return feed.snapshot(index, expiry)


@app.post("/api/sb/quotes")
def quotes(body: dict = Body(...)):
    """Live price + IV/Greeks for each playground leg, grouped by its chain.
    body = {"legs": [{"symbol", "index", "expiry"}, ...]}"""
    legs = body.get("legs") or []
    pairs = {(l.get("index"), l.get("expiry")) for l in legs if l.get("index") in C.INDICES and l.get("expiry")}
    out, chains = {}, {}
    for index, expiry in pairs:
        snap = feed.snapshot(index, expiry)
        if not snap.get("ok"):
            continue
        chains[f"{index}|{expiry}"] = {"fwd": snap["synthetic_fut"], "T_days": snap["T_days"],
                                       "atm": snap["atm"]}
        for r in snap["rows"]:
            for ot in ("CE", "PE"):
                o = r.get(ot)
                if o:
                    out[o["symbol"]] = o
    wanted = {l.get("symbol") for l in legs}
    return {"ok": True, "quotes": {s: out[s] for s in wanted if s in out}, "chains": chains,
            "under": {i: feed.underlying(i) for i in C.INDICES}}


@app.get("/api/sb/strategies")
def strategies(archived: bool = False):
    items = [s for s in store.list_all() if bool(s.get("archived")) == archived]
    return {"ok": True, "strategies": items}


@app.post("/api/sb/strategies")
def save_strategy(body: dict = Body(...)):
    try:
        it = store.upsert(body.get("name"), body.get("index"), body.get("legs"), body.get("id"))
    except ValueError as e:
        return _bad(str(e))
    return {"ok": True, "strategy": it}


@app.post("/api/sb/strategies/{sid}/archive")
def archive_strategy(sid: str, body: dict = Body(default={})):
    if not store.archive(sid, bool(body.get("archived", True))):
        return _bad("not found", 404)
    return {"ok": True}


# ── stage 2: brokers, arming, deploy, positions ──────────────────────────────────────────
def _need_desk():
    if desk is None or hub is None:
        raise RuntimeError("order desk is not running — see logs/strategy_builder.log")


def _call(fn):
    try:
        _need_desk()
        return fn()
    except (ValueError, KeyError, RuntimeError) as e:
        return _bad(str(e))


@app.get("/api/sb/brokers")
def brokers():
    return _call(lambda: {"ok": True, "brokers": hub.status(), "control": desk.armed()})


@app.post("/api/sb/brokers/{aid}/login")
def broker_login(aid: str):
    def go():
        a = hub.get(aid)
        a.login()
        return {"ok": a.status == "logged_in", "broker": a.public(), "error": a.error}
    return _call(go)


@app.get("/api/sb/control")
def control():
    return _call(lambda: {"ok": True, "control": desk.armed()})


@app.post("/api/sb/control")
def set_control(body: dict = Body(...)):
    return _call(lambda: {"ok": True, "control": desk.set_armed(bool(body.get("armed")), "page")})


@app.post("/api/sb/margin")
def margin(body: dict = Body(...)):
    """Zerodha's own basket margin for the legs (Kotak has no basket-margin call — shown as —).
    body = {legs:[...], brokers:{id: mult}, product}"""
    def go():
        from live_trading_options.strategy_builder import brokers as B
        out = {}
        for aid, mult in (body.get("brokers") or {}).items():
            a = hub.get(aid)
            if a.kind != "kite" or int(mult) <= 0:
                out[aid] = {"required": None, "funds": a.funds}
                continue
            try:
                k = a.ensure()
                orders = []
                for l in body.get("legs") or []:
                    c = B.resolve(a, l)
                    orders.append({"exchange": c["exchange"], "tradingsymbol": c["symbol"],
                                   "transaction_type": "BUY" if l["side"] == "B" else "SELL",
                                   "variety": "regular", "product": body.get("product", "NRML"),
                                   "order_type": "MARKET", "quantity": int(l["lots"]) * int(mult) * int(c["lot"]),
                                   "price": 0})
                m = k.basket_order_margins(orders, consider_positions=True, mode="compact")
                out[aid] = {"required": (m.get("final") or m.get("initial") or {}).get("total"),
                            "funds": a.funds}
            except Exception as e:
                out[aid] = {"required": None, "funds": a.funds, "error": str(e)[:200]}
        return {"ok": True, "margin": out}
    return _call(go)


@app.post("/api/sb/deploy")
def deploy(body: dict = Body(...)):
    return _call(lambda: {"ok": True, "deployments": desk.deploy(body)})


@app.get("/api/sb/deployments")
def deployments():
    return _call(lambda: {"ok": True, "deployments": desk.list(), "control": desk.armed()})


@app.post("/api/sb/deployments/{dep_id}/squareoff")
def squareoff(dep_id: str, body: dict = Body(default={})):
    return _call(lambda: (desk.square_off(dep_id, body.get("leg")), {"ok": True})[1])


@app.post("/api/sb/deployments/{dep_id}/cancel")
def cancel_pending(dep_id: str):
    return _call(lambda: (desk.cancel_pending(dep_id), {"ok": True})[1])


@app.post("/api/sb/deployments/{dep_id}/risk")
def set_risk(dep_id: str, body: dict = Body(...)):
    def num(v):
        return float(v) if v not in (None, "", 0, "0") else None
    return _call(lambda: (desk.set_risk(dep_id, num(body.get("sl")), num(body.get("target")),
                                        body.get("exit_time") or None), {"ok": True})[1])


@app.post("/api/sb/killall")
def killall():
    return _call(lambda: {"ok": True, "squaring": desk.square_off_all()})


if __name__ == "__main__":
    import uvicorn
    # binding the port is also the single-instance guard: a second copy fails here
    uvicorn.run(app, host="127.0.0.1", port=C.PORT, log_level="warning")
