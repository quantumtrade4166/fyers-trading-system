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
  GET  /pnl/{name}                   every traded day: day P&L, cumulative, drawdown (today live)
  POST /control/{name}  {"action": "start"|"stop"|"stop_exit"|"exit_all", "expiry_n": 0}
  POST /params/{name}   {"lots":1,"band":15,"normal_band":5,"atm_buffer_pct":20,"mtm_stop":0,"step":0}
  POST /reset/{name}    {"start": true}   archive today's paper book (data/books/{date}/_reset_*) + restart
  POST /auto/{name}     {"auto_start": true, "auto_time": "09:05"}   daily auto-start (saved)
  POST /order/{name}    {"strike":..., "kind":"CE"|"PE", "side":"BUY"|"SELL", "lots":1}   manual paper order
  GET  /live/{name}     LIVE book (NIFTY_LIVE / SENSEX_LIVE): armed, fault, Kotak D margin, every order sent
  POST /live/{name}/arm {"armed": true, "confirm": "LIVE"}   owner only; disarm also STOPS the book (keeps positions)

LIVE: the "{NAME}_LIVE" books send REAL orders to Kotak D (live.py). Everything else stays paper.
"""
import bisect
import datetime as dt
import json
import os
import logging
import socket
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import crypto, days, diag, instruments, st_sim
from live_trading_options.cepe_imbalance.book import IST
from live_trading_options.cepe_imbalance.engine import Engine
from live_trading_options.cepe_imbalance.feed import MultiFeed
from live_trading_options.cepe_imbalance.market import Market
from live_trading_options.cepe_imbalance.writer import W

log = logging.getLogger("cepe.service")
app = FastAPI(title="CE/PE Imbalance (paper)")
E: Engine | None = None
_log_listener = None


def _bad(msg, code=400):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


# ── read-side speed (16-Nov-2026). Display only: nothing below is used by the strategy or by orders. ──
# The page polls /state every 1 s, /chain every 0.4 s, /day every 3 s, /pnl every 10 s - per viewer. Each
# answer was rebuilt from scratch for every caller (the day table re-scanned every 0.5 s sample of every
# book), which kept this process at a full core and left tick processing 2-8 s behind.
_memo: dict = {}                     # key -> (expires_monotonic, value)
_memo_locks: dict = {}
_memo_guard = threading.Lock()
_memo_gen = 0                        # bumped by every control / order: older answers are not stored


def _cached(key, ttl, producer):
    """Answer shared by every caller for `ttl` seconds. One caller builds it; the others wait for that
    one result instead of each building their own (no pile-up)."""
    hit = _memo.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    with _memo_guard:
        lock = _memo_locks.setdefault(key, threading.Lock())
    with lock:
        hit = _memo.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        gen = _memo_gen
        val = producer()
        if gen == _memo_gen:                                # nothing was changed while it was being built
            _memo[key] = (time.monotonic() + ttl, val)
        if len(_memo) > 400:                                # old chain / day keys
            now = time.monotonic()
            for k in [k for k, v in list(_memo.items()) if v[0] <= now]:
                _memo.pop(k, None)
        return val


def _forget():
    """After any control / order / reset: the next read is built fresh."""
    global _memo_gen
    _memo_gen += 1
    _memo.clear()


class _ForgetAfterPost:
    """Every POST (start, stop, exit, params, order, arm, reset) drops the shared answers when it is done."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        try:
            await self.inner(scope, receive, send)
        finally:
            if scope.get("type") == "http" and scope.get("method") == "POST":
                _forget()


app.add_middleware(_ForgetAfterPost)


# ── SEPARATE LIVE ENGINE (17-Nov-2026) ─────────────────────────────────────────────────────────────────
# The paper engine (CEPE_ROLE=paper, 47690) serves the page. Every request about a *_LIVE book is passed
# unchanged to the live engine (CEPE_ROLE=live, 47691), and /state + /day add the live engine's rows, so the
# screen and the dashboard proxy stay exactly as they were.
LIVE_URL = f"http://{C.HOST}:{C.LIVE_PORT}"
_live_last: dict = {"state": None, "at": 0.0, "error": ""}


def _live_call(method, path_qs, body=None, timeout=5.0):
    """-> (status, body bytes, content type). Never raises: an unreachable live engine is a 503."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(LIVE_URL + path_qs, data=body if method != "GET" else None, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("content-type") or "application/json"
    except urllib.error.HTTPError as e:
        return e.code, e.read(), "application/json"
    except Exception as e:                                  # noqa: BLE001 - down / starting / timeout
        return 503, json.dumps({"ok": False, "error": f"live engine not reachable: {e}"}).encode(), "application/json"


def _is_live_path(path: str) -> bool:
    return any(seg.upper().endswith(C.LIVE_SUFFIX) for seg in path.strip("/").split("/")[1:])


class _LiveForward:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if C.ROLE != "paper" or scope.get("type") != "http" or not _is_live_path(scope.get("path", "")):
            return await self.inner(scope, receive, send)
        from starlette.concurrency import run_in_threadpool
        body = b""
        while True:
            m = await receive()
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        qs = scope.get("query_string", b"").decode()
        path = scope["path"] + ("?" + qs if qs else "")
        method = scope["method"]
        status, data, ctype = await run_in_threadpool(_live_call, method, path, body,
                                                      60.0 if method == "POST" else 8.0)
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(data)).encode())]})
        await send({"type": "http.response.body", "body": data})


app.add_middleware(_LiveForward)


def _live_rows():
    """The live engine's /state rows (kept 0.4 s). Unreachable -> the last rows seen, marked engine_down."""
    def fetch():
        st, data, _ = _live_call("GET", "/state", timeout=2.0)
        if st == 200:
            try:
                rows = json.loads(data).get("underlyings") or []
                _live_last.update(state=rows, at=time.time(), error="")
                return rows
            except ValueError:
                _live_last["error"] = "bad answer"
        else:
            try:
                _live_last["error"] = json.loads(data).get("error") or f"HTTP {st}"
            except ValueError:
                _live_last["error"] = f"HTTP {st}"
        rows = [dict(x) for x in (_live_last["state"] or [])]
        age = round(time.time() - _live_last["at"], 1) if _live_last["at"] else None
        for x in rows:
            x["live"] = {**(x.get("live") or {}), "engine_down": True, "engine_error": _live_last["error"],
                         "engine_age_s": age}
            x["blocked"] = f"LIVE ENGINE NOT REACHABLE ({_live_last['error']})"
        return rows
    return _cached("live_rows", 0.4, fetch)


def _fast(obj):
    """Plain json.dumps instead of FastAPI's generic encoder (several times faster on these dicts).
    Anything it cannot write falls back to the generic encoder, exactly as before."""
    try:
        return JSONResponse(obj)
    except (TypeError, ValueError):
        return obj


def _t_of(row):
    return row.get("t", 0)


_day_run: dict = {}                  # book -> running extremes of today's samples
_day_run_lock = threading.Lock()


def _day_stats(nm):
    """Today's MTM / imbalance extremes for one book, kept up to date by reading only the samples that
    arrived since the last call (samples are appended in time order; a reset or a new day swaps in a
    new list, which starts the count again)."""
    h = E.hist.get(nm)
    if h is None:
        return {"last_mtm": None, "hi": None, "lo": None, "imx": None, "imn": None}
    with _day_run_lock:
        s = _day_run.get(nm)
        if s is None or s["list"] is not h:
            s = _day_run[nm] = {"list": h, "t": None, "last_mtm": None,
                                "hi": None, "lo": None, "imx": None, "imn": None}
        if hasattr(h, "extremes"):                      # compact HistBuf: read the columns, build no dicts
            start = 0 if s["t"] is None else h.index_after(s["t"])
            hi, lo, imx, imn, t_last, lm, k = h.extremes(start)
            if k:
                for key, v, better in (("hi", hi, max), ("lo", lo, min), ("imx", imx, max), ("imn", imn, min)):
                    if v is not None:
                        s[key] = v if s[key] is None else better(s[key], v)
                s["t"], s["last_mtm"] = t_last, lm
            return {k2: s[k2] for k2 in ("last_mtm", "hi", "lo", "imx", "imn")}
        new = h[:] if s["t"] is None else h[bisect.bisect_right(h, s["t"], key=_t_of):]
        for x in new:
            m, i = x.get("mtm"), x.get("imb")
            if m is not None:
                if s["hi"] is None or m > s["hi"]:
                    s["hi"] = m
                if s["lo"] is None or m < s["lo"]:
                    s["lo"] = m
            if i is not None:
                if s["imx"] is None or i > s["imx"]:
                    s["imx"] = i
                if s["imn"] is None or i < s["imn"]:
                    s["imn"] = i
        if new:
            s["t"] = _t_of(new[-1])
            s["last_mtm"] = new[-1]["mtm"]
        return {k: s[k] for k in ("last_mtm", "hi", "lo", "imx", "imn")}


def _name(name):
    name = name.upper()
    return name if E and name in E.runners else None


@app.get("/health")
def health():
    if not E:
        return {"ok": False, "error": "starting"}
    f = E.feed.status() if E.feed else {}
    return {"ok": bool(f.get("authed")), "feed": f, "delta_ok": bool((f.get("delta") or {}).get("connected")),
            "universe_date": E.u.file_date, "writer": W.status(), "gc": dict(diag.GC), "role": C.ROLE,
            "live_engine": _live_last if C.ROLE == "paper" else None,
            "rest_backup": E.rest_backup.status() if getattr(E, "rest_backup", None) else None}


@app.get("/debug/prof")
def debug_prof(secs: float = 5.0):
    """Where every thread of this engine spends its time (read-only; see diag.py)."""
    return diag.profile(secs)


@app.get("/debug/stacks")
def debug_stacks():
    return diag.stacks()


@app.get("/state")
def state():
    return _cached("state", 0.4, lambda: _fast(_state()))


def _state():
    out = E.summary()
    if C.ROLE == "paper":
        out["underlyings"] = out["underlyings"] + _live_rows()
        out["live_engine"] = {"ok": not _live_last["error"], "error": _live_last["error"],
                              "age_s": round(time.time() - _live_last["at"], 1) if _live_last["at"] else None}
    return out


@app.get("/chain/{name}")
def chain(name: str, n: int = 10, expiry_n: int = 0, grid: int = 1):
    """Live chain around ATM. grid=1 keeps only the strategy's strike grid; rows carry our held lots."""
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    return _cached(("chain", nm, n, expiry_n, grid), 0.2, lambda: _fast(_chain(nm, n, expiry_n, grid)))


def _chain(nm, n, expiry_n, grid):
    r = E.runners[nm]
    u = r.und                                             # NIFTY_ST shows NIFTY's chain + its own lots
    e = E.market.expiry(u, expiry_n)
    n = max(1, min(n, 30))
    step = float(r.params.get("step") or C.STRIKE_STEP.get(u) or 0)
    rows = []
    if e:
        listed = E.u.underlyings[u].strikes(e)
        gap = min((b - a for a, b in zip(listed, listed[1:])), default=step) or step
        ratio = max(1, int(round(step / gap))) if grid and step else 1
        center = r.state.get("atm") or E.market.atm(u, e)
        rows = E.market.chain_rows(u, e, n * ratio)
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
    return {"name": nm, "expiry": e, "expiries": E.u.underlyings[u].expiries()[:4], "step": step,
            "ref": E.market.ref_price(u, e), "atm": r.state.get("atm") or E.market.atm(u, e), "rows": rows}


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


@app.get("/stwarm/{name}")
def st_warm(name: str):
    """Previous-session closed 1-min ATM CE+PE candles (same expiry) the Supertrend signal warmed up on.
    14-Nov: the normal book's chart seeds its own Supertrend with these, so its line matches the strategy's."""
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    s = E.signals.get(C.und_of(nm))
    if not s:
        return {"name": nm, "bars": [], "source": "none"}
    with s.lock:
        bars, src, wd = list(s.warm), s.warm_source, s.warm_day
    return JSONResponse({"name": nm, "date": E.runners[nm].date, "bars": bars, "source": src, "warm_day": wd})


@app.get("/history/{name}")
def history(name: str, date: str = "", since: float = 0, max_points: int = 0):
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    date = date or E.runners[nm].date
    if date != E.runners[nm].date and C.is_variant(nm):
        sd = st_sim.sim_digest(date, nm)                  # variant did not run that day: simulated from normal
        if sd:
            rows = days.downsample(sd["ds"], max_points) if max_points > 0 else sd["ds"]
            return JSONResponse({"name": nm, "date": date, "rows": rows,
                                 "sim": {"pnl": sd["pnl"], "warm_day": sd["warm_day"]}})
    if date != E.runners[nm].date and not since and 0 < max_points <= days.DS_POINTS:
        s = days.hist_stats(date, nm)                     # past day: cached chart copy, no full parse
        rows = days.downsample(s["ds"], max_points) if s else []
    else:
        today = date == E.runners[nm].date
        if today and not since and max_points > 0:
            # 17-Nov: the whole-day chart (1 MB) is built once and shared for 2 s - opening a book, a second
            # screen or a reload no longer each rebuild it while the engine is busy with prices
            # 20-Nov: 10 s (was 2 s) - the page then asks only for samples newer than its last one
            return _cached(("hist", nm, date, max_points), 10.0, lambda: _hist_full(nm, date, max_points))
        rows = E.hist.get(nm, []) if today else days.history(date, nm)
        if since and hasattr(rows, "since"):
            rows = rows.since(since)                      # compact HistBuf: only the new rows become dicts
        elif since:                                       # samples are in time order: jump to the new ones
            rows = rows[bisect.bisect_right(rows, since, key=_t_of):]
        elif today:
            rows = list(rows)
        if max_points > 0:
            rows = days.downsample(rows, max_points)
    # plain json.dumps: FastAPI's generic encoder took ~4 s for 8000 rows (13-Nov)
    return JSONResponse({"name": nm, "date": date, "rows": rows})


def _hist_full(nm, date, max_points):
    h = E.hist.get(nm, [])
    rows = days.downsample(h if hasattr(h, "downsample") else list(h), max_points)
    return JSONResponse({"name": nm, "date": date, "rows": rows})


@app.get("/days")
def day_list():
    return _cached("days", 20.0, _day_list)


def _day_list():
    today = next(iter(E.runners.values())).date
    live = {r.date for r in E.runners.values()}           # BTC full expiry is already on tomorrow after 17:30
    return {"today": today, "days": sorted(set(days.list_days()) | live, reverse=True)}


@app.get("/day/{date}")
def day(date: str):
    """EOD P&L table for every instrument on one day (today = live numbers).
    date "live" = every book's CURRENT day (BTC full expiry is on tomorrow's book from 17:30)."""
    return _cached(("day", date), 2.0, lambda: _fast(_day(date)))


def _day(date):
    rows = []
    want = date
    for nm, r in list(E.runners.items()):
        date = r.date if want == "live" else want
        s = days.summary(date, nm, stats=_day_stats(nm) if date == r.date else None)
        s["date"] = date
        if date == r.date:                                 # today: live MTM, not the last file sample
            m = r.book.mtm()
            s.update(pnl=m["total"], realized=m["realized"], open_lots=sum(abs(p.lots) for p in r.book.positions.values()),
                     fills=len(r.book.trades), traded=bool(r.book.trades), running=r.running, phase=r.state.get("phase"))
        s["ccy"] = C.ccy_of(nm)
        rows.append(s)
    if C.ROLE == "paper":                                  # the live engine's books on that day
        st, data, _ = _live_call("GET", f"/day/{want}", timeout=3.0)
        if st == 200:
            try:
                rows += json.loads(data).get("rows") or []
            except ValueError:
                pass
    # rupees and dollars are never added together: BTC books (USD) have their own total
    return {"date": want, "rows": rows, "total": round(sum(x["pnl"] for x in rows if x["ccy"] == "INR"), 2),
            "total_usd": round(sum(x["pnl"] for x in rows if x["ccy"] == "USD"), 2)}


@app.get("/pnl/{name}")
def pnl(name: str):
    """Day-by-day P&L of one instrument (today = live MTM) + cumulative + drawdown."""
    nm = _name(name)
    if not nm:
        return _bad("unknown underlying", 404)
    return _cached(("pnl", nm), 5.0, lambda: _pnl(nm))


def _pnl(nm):
    r = E.runners[nm]
    live = r.book.mtm()["total"] if r.book.trades else None
    rows = days.pnl_series(nm, today=r.date, today_pnl=live)
    return {"name": nm, "today": r.date, "ccy": C.ccy_of(nm), "rows": rows}


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
        with E.lock_for(nm):
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


@app.get("/live/{name}")
def live_view(name: str):
    nm = _name(name)
    if not nm or not E.runners[nm].live:
        return _bad("not a live book", 404)
    r = E.runners[nm]
    return {"name": nm, **r.book.live_view(), "orders_all": r.book.orders[-500:]}


@app.post("/live/{name}/arm")
def live_arm(name: str, body: dict = Body(...)):
    nm = _name(name)
    if not nm or not E.runners[nm].live:
        return _bad("not a live book", 404)
    want = bool(body.get("armed"))
    if want and str(body.get("confirm", "")).strip().upper() != "LIVE":
        return _bad("type LIVE to arm")
    if want:                                                # owner 17-Nov: one live index a day
        other = [n for n in E.runners if n != nm and E.runners[n].live and E.live.control.armed(n)]
        if other:
            return _bad(f"{other[0]} is armed - disarm it first (only one live index at a time)")
    st = E.live.control.set(nm, want, by=str(body.get("by") or "screen")[:40])
    out = {"ok": True, "armed": st["armed"]}
    if want:
        E.refresh_subscriptions()                           # live engine: start this index's prices now
    if not want and E.runners[nm].running:
        out["stop"] = E.stop(nm)                            # disarm = no new risk: rules stop, positions kept
    E.runners[nm].event("live_arm", armed=want)
    return out


def main():
    import uvicorn
    from live_trading_options.mcx_terminal import kotak_session
    from live_trading_options.mcx_terminal import config as MC

    # 17-Nov split: the service is always ONE of the two processes. No role given = the paper engine, which
    # never builds a live book - a live book can never run in two processes at once.
    if C.ROLE not in ("paper", "live"):
        C.ROLE = "paper"
    C.DATA.mkdir(parents=True, exist_ok=True)
    C.LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 17-Nov: log lines go through a queue; the file / console are written by the queue's own thread, so a
    # busy disk cannot hold up the tick worker or a live book in the middle of a log line.
    import logging.handlers
    import queue as _queue
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    logname = "cepe_live.log" if C.ROLE == "live" else "cepe_imbalance.log"
    sinks = [logging.FileHandler(C.LOG_DIR / logname, encoding="utf-8"), logging.StreamHandler(sys.stdout)]
    for h in sinks:
        h.setFormatter(fmt)
    logq = _queue.Queue(-1)
    qh = logging.handlers.QueueHandler(logq)
    qh.setFormatter(logging.Formatter("%(message)s"))      # the sinks add time / level / name
    logging.basicConfig(level=logging.INFO, handlers=[qh])
    # Python lets a busy thread keep going for 5 ms before another may run. The price-socket reader needs
    # the baton back after EVERY network read, so while any other thread was busy (rules, a chart request)
    # it got ~200 reads a second at most, fell behind the stream and the feed was dropped as dead.
    # 1 ms keeps the reader (and the screen's answers) moving; the cost is a few % of raw speed.
    sys.setswitchinterval(0.001)
    global _log_listener
    _log_listener = logging.handlers.QueueListener(logq, *sinks)
    _log_listener.start()
    # single instance: refuse if the port is taken
    port = C.port_for()
    s = socket.socket()
    try:
        s.bind((C.HOST, port))
    except OSError:
        log.error("port %s busy: another CE/PE imbalance engine (%s) is running", port, C.ROLE)
        sys.exit(1)
    s.close()

    # start-up builds millions of long-lived objects (contract list, today's chart samples): the automatic
    # memory clean-up re-scanning them again and again made a restart ~30 s slower. Off while loading.
    import gc
    gc.disable()
    MC.ENV_SUFFIX = C.ENV_SUFFIX                          # Kotak P login (same account as the MCX terminal)
    sessions = kotak_session.SessionHolder()
    sess = sessions.get()
    live_role = C.ROLE == "live"
    if live_role:                                         # NIFTY / SENSEX only, own scrip cache files
        u = instruments.load(sess.client, segs=C.LIVE_SEGS, prefix="live_scrip_")
    else:
        u = instruments.load(sess.client)
        crypto.load_into(u)                               # BTC options from Delta (public, no key)
    market = Market(u)
    live_ctx = None
    if C.ROLE != "paper":
        from live_trading_options.cepe_imbalance import live as LV     # LIVE books on Kotak D (same login)
        live_ctx = LV.LiveCtx(LV.Account(sessions))
    global E
    E = Engine(u, market, live=live_ctx, role=C.ROLE)
    kotak_feed = MultiFeed(sessions, E.on_ticks)
    feed = kotak_feed if live_role else crypto.CompositeFeed(kotak_feed, crypto.DeltaFeed(E.on_ticks))
    E.kite = None
    if os.getenv("CEPE_KITE_ASSIST", "1") == "1":         # 25-Nov: Kite prices too (local tick bus), newest wins
        try:
            from live_trading_options.feedlink import kite_assist as KA
            E.kite = KA.for_cepe(u, market, E.on_ticks, ("NFO", "BFO") if live_role else ("NFO", "BFO", "MCX"),
                                 cap=int(os.getenv("CEPE_KITE_CAP", "300" if live_role else "900")),
                                 tag=f"cepe-{C.ROLE}")
            feed = KA.DualFeed(feed, E.kite)
        except Exception as e:
            log.warning("Kite assist off (Kotak only): %s", e)
    E.feed = feed
    E.refresh_subscriptions()                             # spots + futures first; windows once ATM is known
    diag.gc_guard()                                       # everything loaded so far is permanent (see diag.py)
    gc.enable()
    if not live_role:
        W.start()                                         # paper-book + chart files: written off the hot path
    diag.start_watchdog(E, lambda: kotak_feed)
    feed.start()
    if live_role:                                         # 25-Nov: REST quotes while Kotak's stream is silent
        from live_trading_options.cepe_imbalance.rest_backup import RestBackup
        E.rest_backup = RestBackup(E, kotak_feed, sessions)
        E.rest_backup.start()
    E.run_timer()
    import threading                                      # past-day digests, so the day table is instant
    if E.runners and not live_role:
        threading.Thread(target=days.warm, args=(list(E.runners), next(iter(E.runners.values())).date),
                         daemon=True, name="CepeDaysWarm").start()

    def daily_exit():                                     # 08:45 IST next day -> exit 3; the task starts a
        import os, time as _t                             # fresh engine (new Kotak login + contract list)
        born = dt.datetime.now(IST).date()
        while True:
            _t.sleep(30)
            now = dt.datetime.now(IST)
            if now.date() > born and now.strftime("%H:%M") >= "08:45":
                log.info("daily restart (exit 3)")
                W.flush(15)                               # every queued file write and log line first
                try:
                    _log_listener.stop()
                except Exception:
                    pass
                os._exit(3)
    import threading
    threading.Thread(target=daily_exit, daemon=True, name="CepeDailyExit").start()
    log.info("CE/PE imbalance %s engine up on %s:%s (%d books)", C.ROLE, C.HOST, port, len(E.runners))
    uvicorn.run(app, host=C.HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
