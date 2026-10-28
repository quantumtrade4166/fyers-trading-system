"""
mcx_terminal/sched.py — EXIT BY TIME: "exit X % of <instrument> at HH:MM:SS (IST)".
================================================================================

Runs inside the engine (VPS), so a timer fires even when no screen is open. One timer per
(instrument, %) — setting it again replaces the time. Each fires ONCE, on the book that was active
when it was set (a timer set in LIVE exits LIVE positions = what Kotak holds). Timers are for today
only: saved to data/sched_exits_YYYY-MM-DD.json, so they survive an engine restart but never carry
into the next day (the 00:05 reset starts a new file).
"""
import datetime as dt
import json
import logging
import re
import threading

from live_trading_options.mcx_terminal import audit
from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.sched")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
FRACS = (0.25, 0.5, 0.75, 1.0)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")


def parse_time(s: str):
    """'9:5' no; '09:05' / '23:20:00' -> 'HH:MM:SS' or None."""
    m = TIME_RE.match(str(s or "").strip())
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3) or '00'}"


class ExitTimers:
    def __init__(self, fire, on_change=None, path=None, now=None):
        """fire(mode, name, frac) -> result dict (does the actual exit)."""
        self.fire = fire
        self.on_change = on_change or (lambda: None)
        self.now = now or (lambda: dt.datetime.now(IST))
        self.path = path or (C.DATA / f"sched_exits_{self.now().date().isoformat()}.json")
        self.lock = threading.Lock()
        self.items: list = []
        try:
            if self.path.exists():
                self.items = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("exit timers load failed")

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items), encoding="utf-8")
        tmp.replace(self.path)

    def view(self) -> list:
        with self.lock:
            return [dict(x) for x in self.items]

    def set(self, name: str, frac: float, at: str, mode: str, user: str = "") -> dict:
        frac = float(frac)
        if frac not in FRACS:
            return {"ok": False, "msg": "exit % must be 25, 50, 75 or 100"}
        t = parse_time(at)
        if not t:
            return {"ok": False, "msg": "time must be HH:MM or HH:MM:SS (24h)"}
        if t <= self.now().strftime("%H:%M:%S"):
            return {"ok": False, "msg": f"{t} has already passed today"}
        pct = int(frac * 100)
        with self.lock:
            self.items = [x for x in self.items if not (x["name"] == name and x["frac"] == frac and x["state"] == "armed")]
            self.items.append({"id": f"{name}-{pct}-{t}", "name": name, "frac": frac, "at": t, "mode": mode,
                               "by": user, "state": "armed", "result": ""})
            self._save()
        audit.write("timer", user, what=f"EXIT {pct}% {name} set for {t} ({mode})")
        self.on_change()
        return {"ok": True, "msg": f"Exit {pct}% of {name} at {t} IST ({mode}) - armed"}

    def clear(self, name: str, frac: float, user: str = "") -> dict:
        frac = float(frac)
        with self.lock:
            n = len(self.items)
            self.items = [x for x in self.items if not (x["name"] == name and x["frac"] == frac and x["state"] == "armed")]
            removed = n - len(self.items)
            if removed:
                self._save()
        if not removed:
            return {"ok": False, "msg": "no timer set there"}
        audit.write("timer", user, what=f"EXIT {int(frac * 100)}% {name} timer cleared")
        self.on_change()
        return {"ok": True, "msg": f"Exit {int(frac * 100)}% {name} timer cleared"}

    def tick(self):
        """Called every 0.5 s by the engine: fire every armed timer whose time has come."""
        now = self.now().strftime("%H:%M:%S")
        with self.lock:
            due = [x for x in self.items if x["state"] == "armed" and x["at"] <= now]
            for x in due:
                x["state"] = "firing"
            if due:
                self._save()
        for x in due:
            pct = int(x["frac"] * 100)
            audit.write("timer", x.get("by", ""), what=f"EXIT {pct}% {x['name']} FIRING (timer {x['at']}, {x['mode']})")
            try:
                r = self.fire(x["mode"], x["name"], x["frac"])
            except Exception as e:
                log.exception("timer exit")
                r = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
            with self.lock:
                x["state"] = "done" if r.get("ok") else "failed"
                x["result"] = str(r.get("msg", ""))[:300]
                self._save()
            log.warning("timer exit %s %d%% at %s -> %s", x["name"], pct, x["at"], x["result"])
            audit.write("timer", x.get("by", ""), what=f"EXIT {pct}% {x['name']} timer result", ok=r.get("ok"),
                        result=x["result"])
        if due:
            self.on_change()
        return due
