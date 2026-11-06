"""
mcx_terminal/mtm.py — today's Session P&L curve (engine-side), for the MTM graph.
================================================================================

The engine samples Session P&L every SAMPLE_S seconds while the day has any trade, in total and
per product (CRUDEOIL / GOLDM / SILVERM), plus the short-premium balance: premium of short calls
(CE) vs short puts (PE) at the moment and the imbalance (CE - PE) / (CE + PE) in %. The screen asks for it at one point per N minutes
(each bucket keeps its LOW, HIGH and LAST sample, in time order, so the line and the High / Low
numbers always show the true extremes). Between the 5-second points the engine checks P&L every
0.5 s (`watch`): a value that beats the day's high or low so far is stored as an extra point, so a
short spike is never lost. Saved to data/mtm_YYYY-MM-DD.json every minute, so a restart
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
        self._win: dict = {}                         # extremes seen since the last stored point (watch)
        try:
            if self.path.exists():
                self.pts = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("mtm history load failed")
        self._hl = self._extremes(self.pts)

    @staticmethod
    def _extremes(pts) -> dict:
        """series ("all" or a product) -> [low, high] over the stored points."""
        hl: dict = {}
        for pt in pts:
            for k, v in [("all", pt[1])] + list(pt[2].items()):
                h = hl.setdefault(k, [v, v])
                h[0], h[1] = min(h[0], v), max(h[1], v)
        return hl

    def _row(self, positions, ltp_of, now):
        """One sample [t, total, {name: pnl}, {ce, pe, imb}] or None (a held contract has no price yet)."""
        per: dict = {}
        ce = pe = 0.0
        for p in positions:
            if p["net"] and not ltp_of(p["token"]):
                return None                          # a held contract has no price yet: no false dip on the chart
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
        return [round(now, 1), round(sum(per.values()), 2), {k: round(v, 2) for k, v in per.items()},
                {"ce": round(ce, 2), "pe": round(pe, 2), "imb": imb}]

    def _beats(self, row) -> bool:
        for k, v in [("all", row[1])] + list(row[2].items()):
            h = self._hl.get(k)
            if h is None or v < h[0] or v > h[1]:
                return True
        return False

    def _store(self, row):
        self.pts.append(row)
        for k, v in [("all", row[1])] + list(row[2].items()):
            h = self._hl.setdefault(k, [v, v])
            h[0], h[1] = min(h[0], v), max(h[1], v)

    def watch(self, positions, ltp_of, now=None):
        """Every 0.5 s between samples: remember the window's extremes (stored at the next sample, only
        if they beat the day's low / high so far - total or any one product)."""
        if not positions:
            return
        row = self._row(positions, ltp_of, now or time.time())
        if row is None:
            return
        with self.lock:
            if not self._beats(row):
                return
            w = self._win
            if "lo" not in w or row[1] < w["lo"][1]:
                w["lo"] = row
            if "hi" not in w or row[1] > w["hi"][1]:
                w["hi"] = row
            for k, v in row[2].items():               # each product's own extremes in the window
                if f"lo:{k}" not in w or v < w[f"lo:{k}"][2][k]:
                    w[f"lo:{k}"] = row
                if f"hi:{k}" not in w or v > w[f"hi:{k}"][2][k]:
                    w[f"hi:{k}"] = row

    def sample(self, positions, ltp_of, now=None):
        if not positions:
            self._win = {}
            return
        now = now or time.time()
        row = self._row(positions, ltp_of, now)
        with self.lock:
            extra = {r[0]: r for r in self._win.values()}
            self._win = {}
            for t in sorted(extra):                   # window extremes first, in time order
                r = extra[t]
                if (row is None or t < row[0]) and self._beats(r):
                    self._store(r)
            if row is None:
                return
            self._store(row)
            if now - self._saved >= 60:
                self._saved = now
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.pts), encoding="utf-8")
                tmp.replace(self.path)

    def reset(self):
        """Start today's curve again from nothing (paper reset)."""
        with self.lock:
            self.pts = []
            self._saved = 0.0
            self._win, self._hl = {}, {}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("[]", encoding="utf-8")
            tmp.replace(self.path)

    def query(self, mins: float = 1, inst: str = "all", kind: str = "pnl") -> dict:
        """kind "pnl": Session P&L (total or one instrument) · kind "imb": CE/PE imbalance % (+ce/pe premium)."""
        step = max(float(mins), 0.1) * 60
        with self.lock:
            pts = list(self.pts)
        names = sorted({k for pt in pts for k in pt[2]})
        rows = []
        for pt in pts:
            t, tot, per = pt[0], pt[1], pt[2]
            if kind == "imb":
                x = pt[3] if len(pt) > 3 else {}
                if x.get("imb") is None:
                    continue
                rows.append([t, x["imb"], x["ce"], x["pe"]])
            else:
                v = tot if inst == "all" else per.get(inst)
                if v is not None:
                    rows.append([t, v])
        res = {"points": bucket(rows, step), "instruments": names, "mins": mins, "inst": inst, "kind": kind}
        if rows:                                     # the day's true extremes (every stored point, not the buckets)
            res["hi"] = max(rows, key=lambda r: r[1])[:2]
            res["lo"] = min(rows, key=lambda r: r[1])[:2]
        return res


def bucket(rows, step) -> list:
    """Down-sample [t, v, ...] rows to buckets of `step` seconds, keeping each bucket's LOW, HIGH and LAST
    row in time order - the line never cuts off a peak or a dip."""
    out, cur, last_b = [], [], None

    def flush():
        if cur:
            keep = {id(min(cur, key=lambda r: r[1])), id(max(cur, key=lambda r: r[1])), id(cur[-1])}
            out.extend(r for r in cur if id(r) in keep)

    for r in rows:
        b = int(r[0] // step)
        if b != last_b:
            flush()
            cur, last_b = [], b
        cur.append(r)
    flush()
    return out


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
