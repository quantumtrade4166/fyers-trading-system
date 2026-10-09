"""
btc_vwap_local.py -- BTC VWAP only dashboard for running on the local PC while the VPS
is unavailable (added 2026-10-10).

Serves the normal dashboard page (deployment/static/index.html) opened on the BTC VWAP
tab with every other tab hidden, plus ONLY the BTC VWAP API (deployment/btc_vwap_api.py).
Nothing else from main.py is started: no scheduler, no Indian-market engines, no broker
logins, no Fyers feed. Other endpoints the page may poll answer 503 quickly.

Bound to 127.0.0.1 only (the page can ARM live trading, so it must not be reachable
from the network).

Run:  .venv\\Scripts\\python.exe deployment\\btc_vwap_local.py   ->  http://127.0.0.1:8100/
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from deployment.btc_vwap_api import router as btcvwap_router  # noqa: E402

STATIC = ROOT / "deployment" / "static"
PORT = 8100

# Opens the BTC VWAP tab, hides the others, and labels the page as the local copy.
_INJECT = """
<style>
  #tab-bar .tab-btn:not(#tab-btcvwap) { display: none !important; }
  #local-banner { background:#3a2a00; color:#ffd479; padding:6px 12px; font:13px sans-serif;
                  border-bottom:1px solid #ffb300; }
</style>
<script>
  window.addEventListener('load', function () {
    var b = document.createElement('div');
    b.id = 'local-banner';
    b.textContent = 'LOCAL PC copy - BTC VWAP only (VPS unavailable). Keep this PC awake while a live session is open.';
    document.body.insertBefore(b, document.body.firstChild);
    try { switchTab('btcvwap'); } catch (e) { console.error(e); }
  });
</script>
</body>"""

app = FastAPI(title="BTC VWAP (local)", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(btcvwap_router)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return html.replace("</body>", _INJECT, 1)


@app.websocket("/ws")
async def ws_idle(ws: WebSocket) -> None:
    # The page opens /ws for other tabs; accept and stay quiet so it does not reconnect-loop.
    await ws.accept()
    try:
        while True:
            await ws.receive_text()
    except Exception:
        pass


@app.api_route("/api/{rest:path}", methods=["GET", "POST"])
def not_here(rest: str) -> JSONResponse:
    return JSONResponse({"detail": "not available in BTC-only local mode"}, status_code=503)


if __name__ == "__main__":
    print(f"BTC VWAP local dashboard -> http://127.0.0.1:{PORT}/", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
