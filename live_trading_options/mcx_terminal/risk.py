"""
mcx_terminal/risk.py — Session-P&L guard (stop-loss / target / trailing), runs on the VPS.
========================================================================================

Same model as Neo Desk's risk panel, but it lives in the ENGINE, so it still works when the
screen, laptop or Wi-Fi is gone.

  Session P&L = realized + open MTM of every paper position (rupees, multiplier applied).
  stop_loss : exit everything when Session P&L <= this level   (-5000 = stop at a 5k loss)
  target    : exit everything when Session P&L >= this level   (fixed, never trailed)
  trail     : every time Session P&L rises `trail` above where the guard was set / last raised,
              the stop-loss level is raised by `trail`. With no stop typed, the stop starts at
              (P&L at Set) - trail.

A trigger fires EXIT ALL once (cancel open orders + close every position), records why, and
disarms. Pressing Set again re-arms. State is saved to data/risk_YYYY-MM-DD.json.

SAFETY (30-Oct: a restart fired the stop on a FALSE -18,150 while the real P&L was +5,950 - the
losing legs had prices, the winning legs had none yet, and a leg with no price simply did not count):
  * no decision unless EVERY open position has a price and (live) Kotak positions are fresh
  * a stop / target must hold CONFIRM_S on repeated readings before EXIT ALL fires
  * Set is refused while prices are incomplete (the trail base would be wrong)
"""
import datetime as dt
import json
import logging
import threading
import time

from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.risk")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CONFIRM_S = 1.0          # a stop / target must still be crossed this long after it was first seen


def pnl_detail(positions, ltp_of):
    """-> (Session P&L, [symbols of open positions with NO price]). With anything missing the total
    is NOT the real P&L (that leg's open MTM is left out) and must not be acted on."""
    total, missing = 0.0, []
    for p in positions:
        total += p["realized"]
        if p["net"]:
            ltp = ltp_of(p["token"])
            if ltp is None or ltp <= 0:
                missing.append(p.get("symbol") or str(p["token"]))
            else:
                total += (ltp - p["avg"]) * p["net"] * p["mult"]
    return total, missing


def session_pnl(positions, ltp_of) -> float:
    return pnl_detail(positions, ltp_of)[0]


class RiskGuard:
    def __init__(self, book, ltp_of, on_change=None, path=None):
        self.book = book
        self.ltp_of = ltp_of
        self.on_change = on_change or (lambda: None)
        self.path = path or (C.DATA / f"risk_{dt.datetime.now(IST).date().isoformat()}.json")
        self.lock = threading.Lock()
        self.s = {"armed": False, "stop": None, "target": None, "trail": None, "base": None,
                  "best": None, "set_at": None, "fired": None, "pnl": 0.0}
        self._breach = None       # (kind, first seen) of a crossed stop / target waiting for CONFIRM_S
        self._waiting_logged = 0.0
        self._load()

    def _ready(self, positions):
        """-> (pnl, why-not) : why-not is None when the P&L can be trusted."""
        pnl, missing = pnl_detail(positions, self.ltp_of)
        if hasattr(self.book, "positions_fresh") and not self.book.positions_fresh():
            return pnl, "Kotak positions not read yet"
        if missing:
            return pnl, "no price yet for " + ", ".join(missing[:4]) + (" ..." if len(missing) > 4 else "")
        return pnl, None

    def _load(self):
        try:
            if self.path.exists():
                self.s.update(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            log.exception("risk state load failed")

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s), encoding="utf-8")
        tmp.replace(self.path)

    def state(self) -> dict:
        with self.lock:
            return dict(self.s)

    def set(self, stop=None, target=None, trail=None) -> dict:
        pnl, why = self._ready(self.book.positions())
        if why:
            return {"ok": False, "msg": f"MTM guard not set: {why} - try again in a moment"}
        stop = float(stop) if stop not in (None, "") else None
        target = float(target) if target not in (None, "") else None
        trail = float(trail) if trail not in (None, "") else None
        if trail is not None and trail <= 0:
            return {"ok": False, "msg": "trail step must be above 0"}
        if stop is None and trail is not None:
            stop = pnl - trail
        if stop is None and target is None:
            return {"ok": False, "msg": "type a stop-loss and/or a target"}
        if stop is not None and pnl <= stop:
            return {"ok": False, "msg": f"Session P&L {pnl:,.0f} is already at/below the stop {stop:,.0f}"}
        if target is not None and pnl >= target:
            return {"ok": False, "msg": f"Session P&L {pnl:,.0f} is already at/above the target {target:,.0f}"}
        with self.lock:
            self.s.update({"armed": True, "stop": stop, "target": target, "trail": trail, "base": pnl,
                           "best": pnl, "set_at": dt.datetime.now(IST).strftime("%H:%M:%S"), "fired": None,
                           "pnl": pnl})
            self._save()
        log.info("risk armed: stop %s target %s trail %s (P&L at set %.2f)", stop, target, trail, pnl)
        self.on_change()
        return {"ok": True, "msg": "MTM guard armed"}

    def clear(self) -> dict:
        with self.lock:
            self.s.update({"armed": False, "stop": None, "target": None, "trail": None, "base": None, "best": None})
            self._save()
        self.on_change()
        return {"ok": True, "msg": "MTM guard cleared"}

    def check(self) -> str | None:
        """Called on ticks of position tokens. -> reason if it fired."""
        positions = self.book.positions()
        pnl, why = self._ready(positions)
        reason = None
        changed = False
        with self.lock:
            s = self.s
            if why:
                # half-loaded prices (a restart, a feed reconnect): the total is not the real P&L
                self._breach = None
                if s["armed"] and time.time() - self._waiting_logged > 30:
                    self._waiting_logged = time.time()
                    log.info("risk guard waiting: %s (P&L so far %.2f not acted on)", why, pnl)
                return None
            s["pnl"] = pnl
            if not s["armed"]:
                self._breach = None
                return None
            if s["best"] is None or pnl > s["best"]:
                s["best"] = pnl
            if s["trail"] and s["stop"] is not None and s["base"] is not None:
                steps = int((pnl - s["base"]) // s["trail"])
                if steps > 0:                                   # raise the stop, never lower it
                    s["stop"] += steps * s["trail"]
                    s["base"] += steps * s["trail"]
                    changed = True
                    log.info("risk trail: stop raised to %.2f (P&L %.2f)", s["stop"], pnl)
            crossed = None
            if s["stop"] is not None and pnl <= s["stop"]:
                crossed = "stop"
            elif s["target"] is not None and pnl >= s["target"]:
                crossed = "target"
            now = time.time()
            if crossed is None:
                self._breach = None
            elif self._breach is None or self._breach[0] != crossed:
                self._breach = (crossed, now)                  # first reading over the line: confirm it
                log.warning("risk %s crossed (P&L %.2f) - confirming for %.1fs", crossed, pnl, CONFIRM_S)
            elif now - self._breach[1] >= CONFIRM_S:
                if crossed == "stop":
                    reason = f"MTM stop-loss: Session P&L {pnl:,.2f} fell to the stop {s['stop']:,.2f}"
                else:
                    reason = f"MTM target: Session P&L {pnl:,.2f} reached the target {s['target']:,.2f}"
                self._breach = None
            if reason:
                s["armed"] = False
                s["fired"] = {"at": dt.datetime.now(IST).strftime("%H:%M:%S"), "reason": reason}
                changed = True
            if changed:
                self._save()
        if reason:
            log.warning("RISK FIRED — %s — exiting all", reason)
            from live_trading_options.mcx_terminal import audit
            audit.write("risk", what=f"RISK FIRED ({type(self.book).__name__}): {reason}")
            res = self.book.exit_all()
            log.warning("risk exit-all result: %s", res.get("msg"))
            audit.write("risk", what=f"risk exit-all result: {res.get('msg')}")
        if changed:
            self.on_change()
        return reason
