"""
mcx_terminal/mtm.py — today's Session P&L curve (engine-side), for the MTM graph.
================================================================================

The engine samples Session P&L every SAMPLE_S seconds while the day has any trade, in total and
per product (CRUDEOIL / GOLDM / SILVERM), plus the short-premium balance: premium of short calls
(CE) vs short puts (PE) at the moment and the imbalance (CE - PE) / (CE + PE) in %. The screen asks for it at one point per N minutes
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
        ce = pe = 0.0
        for p in positions:
            v = p["realized"]
            if p["net"]:
                l = ltp_of(p["token"])
                if l is not None:
                    v += (l - p["avg"]) * p["net"] * p["mult"]
                    if p["net"] < 0 and str(p.get("symbol", "")).endswith("CE"):
                        ce += l * -p["net"] * p["mult"]
                    elif p["net"] < 0 and str(p.get("symbol", "")).endswith("PE"):
                        pe += l * -p["net"] * p["mult"]
            per[p["name"]] = per.get(p["name"], 0.0) + v
        imb = round((ce - pe) / (ce + pe) * 100, 2) if ce + pe > 0 else None
        with self.lock:
            self.pts.append([round(now, 1), round(sum(per.values()), 2), {k: round(v, 2) for k, v in per.items()},
                             {"ce": round(ce, 2), "pe": round(pe, 2), "imb": imb}])
            if now - self._saved >= 60:
                self._saved = now
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.pts), encoding="utf-8")
                tmp.replace(self.path)

    def query(self, mins: float = 1, inst: str = "all", kind: str = "pnl") -> dict:
        """kind "pnl": Session P&L (total or one instrument) · kind "imb": CE/PE imbalance % (+ce/pe premium)."""
        step = max(float(mins), 0.1) * 60
        with self.lock:
            pts = list(self.pts)
        names = sorted({k for pt in pts for k in pt[2]})
        out, last_b = [], None
        for pt in pts:
            t, tot, per = pt[0], pt[1], pt[2]
            if kind == "imb":
                x = pt[3] if len(pt) > 3 else {}
                if x.get("imb") is None:
                    continue
                row = [t, x["imb"], x["ce"], x["pe"]]
            else:
                v = tot if inst == "all" else per.get(inst)
                if v is None:
                    continue
                row = [t, v]
            b = int(t // step)
            if b == last_b:
                out[-1] = row                        # keep the last sample of each bucket
            else:
                out.append(row)
                last_b = b
        return {"points": out, "instruments": names, "mins": mins, "inst": inst, "kind": kind}


class AtmHistory:
    """ATM straddle (call + put at the money, nearest expiry) per instrument, every SAMPLE_S seconds from
    engine start — market data, so it is kept whether or not anything is traded. The ATM strike can move
    during the day; each point keeps the strike it was taken at. Saved to data/atm_YYYY-MM-DD.json."""

    def __init__(self, path=None):
        self.path = path or (C.DATA / f"atm_{dt.datetime.now(IST).date().isoformat()}.json")
        self.pts: dict = {}                      # name -> [[t, straddle, strike], ...]
        self.lock = threading.Lock()
        self._saved = 0.0
        try:
            if self.path.exists():
                self.pts = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("atm history load failed")

    def sample(self, atm: dict, now=None):
        """atm = {name: [strike, straddle, expiry]} (Engine.atm_straddles())."""
        if not atm:
            return
        now = now or time.time()
        with self.lock:
            for name, (k, v, _exp) in atm.items():
                self.pts.setdefault(name, []).append([round(now, 1), v, k])
            if now - self._saved >= 60:
                self._saved = now
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.pts), encoding="utf-8")
                tmp.replace(self.path)

    def query(self, name: str, mins: float = 1) -> dict:
        step = max(float(mins), 0.1) * 60
        with self.lock:
            pts = list(self.pts.get(name, []))
            names = sorted(self.pts)
        out, last_b = [], None
        for t, v, k in pts:
            b = int(t // step)
            if b == last_b:
                out[-1] = [t, v, k]
            else:
                out.append([t, v, k])
                last_b = b
        return {"points": out, "name": name, "instruments": names, "mins": mins}
