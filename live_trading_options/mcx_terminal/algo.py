"""
mcx_terminal/algo.py — the "Algo" tab (owner 23-Nov-2026): the SYNTHETIC TO-AND-FRO strategy on NIFTY / SENSEX.

The strategy runs inside the CE/PE engine (live_trading_options/cepe_imbalance, books NIFTY_SYN / SENSEX_SYN
paper on :47690 and NIFTY_SYN_LIVE / SENSEX_SYN_LIVE live on :47691 - the paper engine forwards *_LIVE paths).
This module only serves the page (/algo) and a NARROW proxy (/algo/api/...) to that engine:
  * only the four SYN books can be read or controlled through it (nothing else of the CE/PE engine)
  * arming LIVE needs the owner's Cloudflare email (same list as the MCX desk's LIVE arm)
The desk's own Access lock (access.py) already guards every path here.
"""
import asyncio
import json
import os
import re
import urllib.error
import urllib.request

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response

from live_trading_options.mcx_terminal import audit
from live_trading_options.mcx_terminal import config as C

ENGINE = os.getenv("CEPE_URL", "http://127.0.0.1:47690")
BOOK = re.compile(r"^(NIFTY|SENSEX)_SYN(_LIVE)?$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GET_BOOK = {"history", "positions", "trades", "events", "live", "pnl", "chain"}
POST_BOOK = {"control", "params", "auto", "reset", "order"}


def _fetch(method, path, qs, body):
    url = f"{ENGINE}/{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, data=body if method == "POST" else None, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _bad(msg, code=400):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


def _allowed(method, parts):
    """(ok, why) for /algo/api/<parts...>."""
    if not parts:
        return False, "no path"
    head = parts[0]
    if method == "GET":
        if parts == ["state"] or parts == ["days"] or parts == ["health"]:
            return True, ""
        if head == "day" and len(parts) == 2 and (parts[1] == "live" or DAY.match(parts[1])):
            return True, ""
        if head in GET_BOOK and len(parts) == 2 and BOOK.match(parts[1].upper()):
            return True, ""
    if method == "POST":
        if head in POST_BOOK and len(parts) == 2 and BOOK.match(parts[1].upper()):
            if head in ("reset", "order") and parts[1].upper().endswith("_LIVE"):
                return False, "not on a LIVE book"
            return True, ""
        if head == "live" and len(parts) == 3 and parts[2] == "arm" and BOOK.match(parts[1].upper()) \
                and parts[1].upper().endswith("_LIVE"):
            return True, ""
    return False, f"{method} /{'/'.join(parts)} is not available on the Algo tab"


def _only_syn(raw: bytes, key: str) -> bytes:
    try:
        doc = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(doc, dict) and isinstance(doc.get(key), list):
        doc[key] = [u for u in doc[key] if isinstance(u, dict) and BOOK.match(str(u.get("name", "")).upper())]
    return json.dumps(doc).encode()


def register(app, owner_users):
    """Add /algo + /algo/api/* to the desk's FastAPI app. owner_users() -> set of emails allowed to arm."""

    @app.get("/algo")
    async def algo_page():
        return FileResponse(C.STATIC / "algo.html", headers={"Cache-Control": "no-cache"})

    @app.api_route("/algo/api/{path:path}", methods=["GET", "POST"])
    async def algo_api(path: str, request: Request):
        parts = [p for p in path.strip("/").split("/") if p]
        ok, why = _allowed(request.method, parts)
        if not ok:
            return _bad(why, 403)
        user = request.scope.get("mcx_user", "local")
        body = await request.body() if request.method == "POST" else b""
        if request.method == "POST":
            try:
                doc = json.loads(body or b"{}")
            except ValueError:
                return _bad("bad JSON")
            if parts[0] == "live":
                if user not in owner_users():
                    return _bad(f"{user} may not arm LIVE", 403)
                doc["by"] = str(user)[:40]
                body = json.dumps(doc).encode()
            try:
                audit.write("algo", user=user, op="/".join(parts), body=doc)
            except Exception:
                pass
        try:
            code, raw = await asyncio.to_thread(_fetch, request.method, "/".join(parts), str(request.url.query), body)
        except (urllib.error.URLError, OSError) as e:
            return _bad(f"strategy engine not reachable ({type(e).__name__}) - it runs as VPS task CepeImbalance", 503)
        if parts == ["state"]:
            raw = _only_syn(raw, "underlyings")
        elif parts[0] == "day":
            raw = _only_syn(raw, "rows")
        return Response(content=raw, status_code=code, media_type="application/json")
