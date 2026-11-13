"""
tick_recorder/service.py — record EVERY tick of NIFTY + SENSEX options for backtesting (owner 13-Nov-2026).
=========================================================================================================

    .venv\\Scripts\\python.exe -m live_trading_options.tick_recorder.service

What is recorded (per index):
  * options of 3 expiries: current week, next week and the monthly (front futures expiry; when that is
    already one of the two weeklies, the NEXT monthly instead, so there are always 3 distinct expiries)
  * strikes within +-STRIKE_BAND of spot (checked every 30 s; strikes are only ever added, so a trending
    day keeps its old strikes too). Deep strikes 5,000+ points away barely trade and would push the
    contract count past one Kotak connection's budget.
  * index spot, the 3 index futures, and India VIX (one feed key)
Feed: Kotak market feed on the Kotak Rohit login (KOTAK_ENV_SUFFIX; same decoder as the CE/PE engine;
its own login: a 2nd login does not end the others, tested 16-Oct). Never places orders.

Files: data/ticks/{date}/{INDEX}_{HHMM}.tcol (one per 15 minutes, ~4 bytes a tick) under C:\\trading ->
backed up to Drive every 10 min by VPSBackupToDrive (whole-root mirror). The live journal *.trec.tmp is
skipped by the backup on purpose (see store.py). Format + reader: tick_recorder/store.py.

Runs 09:00-15:40 IST Mon-Fri: started by the task "TickRecorder" (boot + every 5 min); outside the window
it exits at once without logging in. Single instance = port 127.0.0.1:47693 held while running;
GET /health on that port reports the counts.
"""
import datetime as dt
import json
import logging
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from live_trading_options.tick_recorder.store import TickStore

log = logging.getLogger("ticks.service")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "ticks"
LOG_DIR = ROOT / "logs"
HOST, PORT = "127.0.0.1", 47693
INDICES = ("NIFTY", "SENSEX")
STRIKE_BAND = 0.12
START, STOP = "09:00", "15:40"
EXTRA_KEYS = {"NIFTY": ["nse_cm|India VIX"]}
# Kotak login for the feed: env keys KOTAK_*_ROHIT (owner 13-Nov: keep the recorder's load off Kotak P,
# where the MCX terminal trades live). NOTE: same account + API key as DualMom (KOTAK_DM_*), which
# trades live; a 2nd login on a key does not end the first (tested 16-Oct). Feed only, never orders.
KOTAK_ENV_SUFFIX = "_ROHIT"
BUDGET = 2700


def in_window(now=None) -> bool:
    now = now or dt.datetime.now(IST)
    return now.weekday() < 5 and START <= now.strftime("%H:%M") < STOP


def pick_expiries(und) -> list:
    """Current week, next week, monthly (front futures expiry, or the next one if already taken)."""
    ex = und.expiries()
    out = ex[:2]
    months = [f.expiry for f in und.futures if f.expiry in ex]
    for m in months:
        if m not in out:
            out.append(m)
            break
    return out


def pick_keys(und, spot: float, band: float = STRIKE_BAND) -> list:
    keys = []
    for e in pick_expiries(und):
        for k in und.strikes(e):
            if abs(k - spot) <= band * spot:
                for kind in ("CE", "PE"):
                    c = und.chains[e][k].get(kind)
                    if c:
                        keys.append(c.key)
    return keys


class Recorder:
    def __init__(self, universe, feed_factory):
        self.u = universe
        self.store = TickStore(DATA, self.info)
        self.owner: dict[str, str] = {}               # feed key -> index name
        self.last: dict[str, float] = {}               # spot / futures last price
        self.feed = feed_factory(self.on_ticks)
        self.started = time.time()
        self.ticks = 0

    def info(self, key):
        c = self.u.by_key.get(key)
        if c:
            return c.symbol, c.kind, c.expiry, c.strike
        return key.split("|", 1)[-1], "INDEX", "", 0

    def on_ticks(self, ticks):
        for tk in ticks:
            idx = self.owner.get(tk.key)
            if idx:
                self.store.add(idx, tk)
                if tk.ltp:
                    self.last[tk.key] = tk.ltp
        self.ticks += len(ticks)

    def base_keys(self):
        for n in INDICES:
            und = self.u.underlyings[n]
            keys = ([und.spot_key] if und.spot_key else []) + [f.key for f in und.futures[:3]] + EXTRA_KEYS.get(n, [])
            self.want(n, keys)

    def want(self, index, keys):
        new = [k for k in keys if k not in self.owner]
        room = BUDGET - len(self.owner)
        if len(new) > room:
            log.warning("%s: %d new keys but room for %d", index, len(new), room)
            new = new[:max(room, 0)]
        for k in new:
            self.owner[k] = index
        if new:
            self.feed.want(new)
            log.info("%s: +%d keys (total %d)", index, len(new), len(self.owner))

    def ref(self, index):
        und = self.u.underlyings[index]
        px = self.last.get(und.spot_key) if und.spot_key else None
        if not px and und.futures:
            px = self.last.get(und.futures[0].key)
        return px

    def refresh_strikes(self):
        for n in INDICES:
            px = self.ref(n)
            if px:
                self.want(n, pick_keys(self.u.underlyings[n], px))

    def health(self) -> dict:
        f = self.feed.status()
        return {"ok": bool(f.get("authed")), "login": getattr(self, "login", ""), "feed": f,
                "keys": len(self.owner), "ticks": self.ticks,
                "store": dict(self.store.stats), "up_s": round(time.time() - self.started),
                "spot": {n: self.ref(n) for n in INDICES},
                "expiries": {n: pick_expiries(self.u.underlyings[n]) for n in INDICES}}


def serve_health(rec, sock):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(rec.health()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((HOST, PORT), H, bind_and_activate=False)
    srv.socket.close()
    srv.socket = sock                                  # the already-bound lock socket
    srv.server_activate()
    threading.Thread(target=srv.serve_forever, daemon=True, name="TickHealth").start()


def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(LOG_DIR / "tick_recorder.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    if not in_window():
        return 0                                       # outside 09:00-15:40 Mon-Fri: nothing to do, no login
    sock = socket.socket()
    try:
        sock.bind((HOST, PORT))                        # single instance
    except OSError:
        return 0                                       # already running
    from live_trading_options.cepe_imbalance import instruments
    from live_trading_options.cepe_imbalance.feed import MultiFeed
    from live_trading_options.mcx_terminal import config as MC, kotak_session
    MC.ENV_SUFFIX = KOTAK_ENV_SUFFIX
    sessions = kotak_session.SessionHolder()
    s = sessions.get()
    u = instruments.load(s.client)
    rec = Recorder(u, lambda cb: MultiFeed(sessions, cb))
    rec.login = f"KOTAK{KOTAK_ENV_SUFFIX} (UCC ..{s.ucc[-2:]})"         # which account, without exposing it
    log.info("feed login: %s", rec.login)
    serve_health(rec, sock)
    rec.base_keys()
    rec.feed.start()
    log.info("tick recorder up: expiries %s", {n: pick_expiries(u.underlyings[n]) for n in INDICES})
    try:
        while in_window():
            time.sleep(30)
            rec.refresh_strikes()
            h = rec.health()
            st = h["store"]
            log.info("keys %d  subscribed %s  ticks %d  rows %d  journal MB %.1f  kept MB %.1f  packed %d "
                     "(last %.1f s)  dropped %d  pack errors %d", h["keys"], h["feed"].get("subscribed"), h["ticks"],
                     st["rows"], st["journal_bytes"] / 1e6, st["tcol_bytes"] / 1e6, st["packed"], st["last_pack_s"],
                     st["dropped_frames"], st["pack_errors"])
    finally:
        rec.feed.stop()
        rec.store.stop()
        log.info("tick recorder stopped (window over)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
