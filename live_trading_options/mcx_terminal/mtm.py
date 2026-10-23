"""
mcx_terminal/mtm.py — today's Session P&L curve (engine-side), for the MTM graph.
================================================================================

The engine samples Session P&L every SAMPLE_S seconds while the day has any trade, in total and
per product (CRUDEOIL / GOLDM / SILVERM). The screen asks for it at one point per N minutes
(the last sample in each bucket). Saved to data/mtm_YYYY-MM-DD.json every minute, so a restart
keeps the curve.
"""
import datetime as dt
import json
import logging
import threading
import time

from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.mtm")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SAMPLE_S = 5


class MtmHistory:
    def __init__(self, path=None):
        self.path = path or (C.DATA / f"mtm_{dt.datetime.now(IST).date().isoformat()}.json")
        self.pts: list = []                          # [t, total, {name: pnl}]
        self.lock = threading.Lock()
        self._saved = 0.0
        try:
            if self.path.exists():
                self.pts = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("mtm history load failed")

    def sample(self, positions, ltp_of, now=None):
        if not positions:
            return
        now = now or time.time()
        per: dict = {}
        for p in positions:
            v = p["realized"]
            if p["net"]:
                l = ltp_of(p["token"])
                if l is not None:
                    v += (l - p["avg"]) * p["net"] * p["mult"]
            per[p["name"]] = per.get(p["name"], 0.0) + v
        with self.lock:
            self.pts.append([round(now, 1), round(sum(per.values()), 2), {k: round(v, 2) for k, v in per.items()}])
            if now - self._saved >= 60:
                self._saved = now
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.pts), encoding="utf-8")
                tmp.replace(self.path)

    def query(self, mins: float = 1, inst: str = "all") -> dict:
        step = max(float(mins), 0.1) * 60
        with self.lock:
            pts = list(self.pts)
        names = sorted({k for _, _, per in pts for k in per})
        out, last_b = [], None
        for t, tot, per in pts:
            v = tot if inst == "all" else per.get(inst)
            if v is None:
                continue
            b = int(t // step)
            if b == last_b:
                out[-1] = [t, v]                     # keep the last sample of each bucket
            else:
                out.append([t, v])
                last_b = b
        return {"points": out, "instruments": names, "mins": mins, "inst": inst}
