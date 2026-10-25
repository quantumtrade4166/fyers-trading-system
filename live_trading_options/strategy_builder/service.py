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
import time

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
practice = None             # PracticeManager — replays a past day through the same screen (px=1)


# Every chain / order endpoint takes ?px=1 from the page while practice mode is on. These three
# pick the replay or the live objects — practice can never reach the live desk or a broker.
def bpx(px, body) -> int:
    """practice if EITHER the query or the body says so (belt and braces against a proxy dropping one)."""
    return 1 if px or (isinstance(body, dict) and body.get("px")) else 0


def F(px):
    if px:
        if not (practice and practice.active):
            raise ValueError("practice mode is not running")
        return practice.feed
    return feed


def D(px):
    if px:
        if not (practice and practice.active):
            raise ValueError("practice mode is not running")
        return practice.desk
    _need_desk()
    return desk


def H(px):
    return practice.desk.hub if px and practice and practice.active else hub


def _leg_quotes(legs):
    """Live LTP for deployment legs (keeps their chains watched while positions are open)."""
    # no touch(): open legs stream over the websocket — they must not keep Fyers REST pulls going
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
        paper = O.PaperAdapter(lambda sym: feed.quotes([sym]).get(sym))
        desk = O.Desk(hub, _leg_quotes, {"kite": O.KiteAdapter(B.kx), "kotak": O.KotakAdapter(B.ke),
                                         "paper": paper}, B.resolve)
        hub.start()
        desk.run_monitor()
        global practice
        from live_trading_options.strategy_builder.practice import PracticeManager
        practice = PracticeManager()
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
def meta(px: int = Query(0)):
    try:
        f = F(px)
    except ValueError as e:
        return _bad(str(e))
    return {"ok": True, "indices": [practice.sess["index"]] if px else list(C.INDICES),
            "lot": C.LOT_SIZE, "step": C.STRIKE_STEP,
            "expiries": f.expiries, "under": {i: f.underlying(i) for i in C.INDICES},
            "feed": f.status(), "practice": practice.state() if px else None}


@app.get("/api/sb/chain")
def chain(index: str = Query(...), expiry: str = Query(None), px: int = Query(0)):
    if index not in C.INDICES:
        return _bad(f"unknown index {index}")
    try:
        f = F(px)
    except ValueError as e:
        return _bad(str(e))
    ex = f.expiries.get(index)
    if not px and not ex:
        feed.want_index(index)
    if px and ex:
        expiry = ex[0]["date"]               # a practice day has exactly one expiry
    if not expiry:
        if not ex:
            return {"ok": False, "loading": True, "index": index}
        expiry = ex[0]["date"]
    snap = f.snapshot(index, expiry)
    if px:
        snap["practice"] = practice.state()
    return snap


@app.post("/api/sb/quotes")
def quotes(body: dict = Body(...), px: int = Query(0)):
    px = bpx(px, body)
    """Live price + IV/Greeks for each playground leg, grouped by its chain.
    body = {"legs": [{"symbol", "index", "expiry"}, ...]}"""
    legs = body.get("legs") or []
    pairs = {(l.get("index"), l.get("expiry")) for l in legs if l.get("index") in C.INDICES and l.get("expiry")}
    out, chains = {}, {}
    try:
        f = F(px)
    except ValueError as e:
        return _bad(str(e))
    for index, expiry in pairs:
        snap = f.snapshot(index, expiry)
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
            "under": {i: f.underlying(i) for i in C.INDICES}}


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
def brokers(px: int = Query(0)):
    return _call(lambda: {"ok": True, "brokers": H(px).status(), "control": D(px).armed()})


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
def margin(body: dict = Body(...), px: int = Query(0)):
    px = bpx(px, body)
    """Zerodha's own basket margin for the legs (Kotak has no basket-margin call — shown as —).
    body = {legs:[...], brokers:{id: mult}, product}"""
    def go():
        from live_trading_options.strategy_builder import brokers as B
        out = {}
        if px:
            return {"ok": True, "margin": {a: {"required": None, "funds": None} for a in (body.get("brokers") or {})}}
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
def deploy(body: dict = Body(...), px: int = Query(0)):
    px = bpx(px, body)
    return _call(lambda: {"ok": True, "deployments": D(px).deploy(body)})


@app.get("/api/sb/deployments")
def deployments(px: int = Query(0)):
    return _call(lambda: {"ok": True, "deployments": D(px).list(include_closed_days=10000 if px else 1),
                          "control": D(px).armed()})


@app.post("/api/sb/deployments/{dep_id}/squareoff")
def squareoff(dep_id: str, body: dict = Body(default={}), px: int = Query(0)):
    px = bpx(px, body)
    return _call(lambda: (D(px).square_off(dep_id, body.get("leg")), {"ok": True})[1])


@app.post("/api/sb/deployments/{dep_id}/cancel")
def cancel_pending(dep_id: str, body: dict = Body(default={}), px: int = Query(0)):
    px = bpx(px, body)
    return _call(lambda: (D(px).cancel_pending(dep_id), {"ok": True})[1])


@app.post("/api/sb/deployments/{dep_id}/risk")
def set_risk(dep_id: str, body: dict = Body(...), px: int = Query(0)):
    px = bpx(px, body)
    def num(v):
        return float(v) if v not in (None, "", 0, "0") else None
    return _call(lambda: (D(px).set_risk(dep_id, num(body.get("sl")), num(body.get("target")),
                                        body.get("exit_time") or None), {"ok": True})[1])


@app.post("/api/sb/quick")
def quick(body: dict = Body(...), px: int = Query(0)):
    px = bpx(px, body)
    """Keyboard / Trade-mode order into today's Quick book for that broker (Paper needs no arming)."""
    return _call(lambda: {"ok": True, "deployment": D(px).quick(body)})


@app.get("/api/sb/deployments/{dep_id}/history")
def history(dep_id: str, date: str = Query(None), px: int = Query(0)):
    """MTM + CE/PE imbalance samples (every 5 s) for the day — the EOD charts."""
    return _call(lambda: {"ok": True, "points": D(px).history(dep_id, date)})


@app.post("/api/sb/killall")
def killall(body: dict = Body(default={}), px: int = Query(0)):
    px = bpx(px, body)
    return _call(lambda: {"ok": True, "squaring": D(px).square_off_all()})


# ── practice mode ────────────────────────────────────────────────────────────────────────
@app.get("/api/sb/practice/days")
def practice_days():
    """Prepared days (ready to replay) + the download job, if one is running."""
    from live_trading_options.strategy_builder import practice_data as PD
    return {"ok": True, "packs": PD.list_packs(), "job": _job, "batch": PD.batch_status()}


_job: dict = {"state": "idle"}


@app.post("/api/sb/practice/prepare")
def practice_prepare(body: dict = Body(...)):
    """Download a day from ICICI Breeze in the background (one at a time — Breeze blocks parallel calls)."""
    from live_trading_options.strategy_builder import practice_data as PD
    import threading as _th
    index, date, seconds = body.get("index"), body.get("date"), bool(body.get("seconds"))
    if index not in C.INDICES or not date:
        return _bad("index and date are required")
    if _job.get("state") == "running":
        return _bad(f"already downloading {_job.get('index')} {_job.get('date')} — wait for it to finish")
    if PD.batch_running() and not seconds:
        # never two Breeze downloads at once (parallel calls get the IP blocked) — jump the bulk queue instead
        PD.request_priority(index, date)
        _job.clear()
        _job.update(state="queued", index=index, date=date,
                    lines=["the background 2026 download will fetch this day next (~4 min)"])
        return {"ok": True, "job": _job}
    _job.clear()
    _job.update(state="running", index=index, date=date, seconds=seconds, lines=[], started=time.strftime("%H:%M:%S"))

    def say(msg):
        _job["lines"] = (_job["lines"] + [str(msg)])[-12:]

    def run():
        try:
            b = PD.Builder(index, log=say, fast=True)
            meta = PD.read_meta(index, date)
            if meta and seconds and not meta.get("seconds"):
                b.add_seconds(date)
            elif not meta:
                b.build(date, seconds=seconds)
            _job.update(state="done", finished=time.strftime("%H:%M:%S"))
        except Exception as e:
            _job.update(state="error", error=f"{type(e).__name__}: {e}"[:400])
    _th.Thread(target=run, daemon=True, name="SB-prepare").start()
    return {"ok": True, "job": _job}


def _pcall(fn):
    try:
        if practice is None:
            raise RuntimeError("practice mode is not available — see logs/strategy_builder.log")
        return fn()
    except (ValueError, KeyError, RuntimeError, FileNotFoundError) as e:
        return _bad(str(e))


@app.get("/api/sb/practice/state")
def practice_state():
    return _pcall(lambda: {"ok": True, **practice.state()})


@app.post("/api/sb/practice/start")
def practice_start(body: dict = Body(...)):
    return _pcall(lambda: {"ok": True, **practice.start(body.get("index"), body.get("date"))})


@app.post("/api/sb/practice/resume")
def practice_resume(body: dict = Body(...)):
    return _pcall(lambda: {"ok": True, **practice.resume(body.get("id"))})


@app.post("/api/sb/practice/control")
def practice_control(body: dict = Body(...)):
    def go():
        if not practice.active:
            raise ValueError("practice mode is not running")
        practice.feed.control(body.get("action"), body.get("value"))
        return {"ok": True, **practice.state()}
    return _pcall(go)


@app.post("/api/sb/practice/end")
def practice_end():
    return _pcall(lambda: (practice.end(), {"ok": True, "active": False})[1])


@app.get("/api/sb/practice/sessions")
def practice_sessions():
    return _pcall(lambda: {"ok": True, "sessions": practice.sessions()})


@app.get("/api/sb/practice/sessions/{sid}/combined")
def practice_combined(sid: str):
    return _pcall(lambda: {"ok": True, "points": practice.combined(sid)})


if __name__ == "__main__":
    import uvicorn
    # binding the port is also the single-instance guard: a second copy fails here
    uvicorn.run(app, host="127.0.0.1", port=C.PORT, log_level="warning")
