"""
cepe_imbalance/demo_live_page.py — the CE/PE page on a FAKE Kotak (offline preview of the Live mode).
No Kotak login, no orders: the engine trades a fake broker (test_live.FakeKotak) on toy prices.

    .venv\\Scripts\\python.exe -m live_trading_options.cepe_imbalance.demo_live_page   -> http://127.0.0.1:47699/
"""
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def main():
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    from live_trading_options.cepe_imbalance import instruments as I
    from live_trading_options.cepe_imbalance import live as LV
    from live_trading_options.cepe_imbalance import service
    from live_trading_options.cepe_imbalance import test_live as T

    td = Path(T.__file__).resolve().parent / "testdata"
    texts = {s: (td / f"scrip_{s}_sample.csv").read_text(encoding="utf-8") for s in ("nse_fo", "bse_fo", "mcx_fo", "nse_com")}
    u = I.build(texts, today="2026-11-02")
    for k in ("WAIT_FIRST_S", "WAIT_RETRY_S", "CONFIRM_S"):
        setattr(LV, k, 0.05)
    g = T.Rig(u)
    g.ctx.account.read_limits()
    service.E = g.eng

    def wiggle():
        f = g.f
        while True:
            time.sleep(1)
            f += 7
            g.move(f)
            g.ctx.account.read_positions()
            g.ctx.account.read_limits()
    threading.Thread(target=wiggle, daemon=True).start()

    app = FastAPI()
    app.mount("/api/cepe", service.app)
    page = (Path(__file__).resolve().parents[2] / "deployment" / "static" / "cepe.html").read_text(encoding="utf-8")

    @app.get("/")
    def index():
        return HTMLResponse(page)
    uvicorn.run(app, host="127.0.0.1", port=47699, log_level="warning")


if __name__ == "__main__":
    main()
