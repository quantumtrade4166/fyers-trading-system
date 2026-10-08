"""
ui_demo.py — preview the dashboard tab against REAL engine output, locally.
==========================================================================

Runs the real engine (via sim_test) over a few historical days into a temp
directory, stopping the last day mid-session so an open position and live MTM are
on screen, then serves ONLY index.html + the read-only pivot router from a
throwaway uvicorn on :8765 with the router pointed at that temp data and its clock
frozen at the simulated moment.

It never starts deployment/main.py (that would run schedulers and broker logins).

    python live_trading_options/nifty_pivot/ui_demo.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT))

import sim_test as S                                                              # noqa: E402
from backtesting.options_pivot_intraday.breeze_adapter import BreezeOptionsLoader  # noqa: E402

DEMO = REPO / "logs" / "_ndp_ui_demo"


def build():
    DEMO.mkdir(parents=True, exist_ok=True)
    for f in DEMO.rglob("*"):
        if f.is_file():
            try:
                f.unlink()
            except OSError:
                pass
    loader = BreezeOptionsLoader("NIFTY")
    days = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]
    for d in days:
        S.run_day(loader, d, DEMO)
        print(f"  simulated {d}")
    S.run_day(loader, "2026-06-08", DEMO, stop_at=dt.time(14, 2))
    print("  simulated 2026-06-08 up to 14:02 (live view)")


def serve():
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    import deployment.pivot_api as P

    P.STATE = DEMO / "live_state"
    P.RESULTS = DEMO / "results"
    tick = json.loads((P.STATE / "TICK.json").read_text(encoding="utf-8"))
    frozen = dt.datetime.strptime(f"{tick['date']} {tick['ts']}", "%Y-%m-%d %H:%M:%S") + dt.timedelta(seconds=2)
    P._now = lambda: frozen

    app = FastAPI()
    static = REPO / "deployment" / "static"
    app.mount("/static", StaticFiles(directory=str(static)), name="static")
    app.include_router(P.router)

    @app.get("/")
    def root():
        return FileResponse(str(static / "index.html"))

    for p in ["/api/version", "/api/status", "/api/signals", "/api/positions", "/api/trades",
              "/api/equity", "/api/mode", "/api/ticker", "/api/strangle/status"]:
        app.add_api_route(p, lambda: {}, methods=["GET"])
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    if "--serve-only" not in sys.argv:
        build()
    serve()
