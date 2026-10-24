"""
strategy_builder/practice.py — PRACTICE MODE: replay a past day as if it were live.
==================================================================================

A practice session replays one index on one past day (a pack from practice_data.py) through the
SAME chain screen, Greeks, payoff, paper desk, keyboard, SL/target and charts as live trading —
only the clock and the prices come from history.

PRICES
  * 1-second data (where the pack has it, ATM±5): the real traded price at that second.
  * Otherwise each 1-minute candle is walked open -> low -> high -> close (green candle) or
    open -> high -> low -> close (red candle) over its 60 seconds, so the screen moves every tick.
    The order of the high and the low inside a minute is the one guess in the replay.
  * No historical bid/ask exists anywhere, so the quote is LTP -/+ a half-spread that widens with
    the premium (SPREAD below); paper fills hit that bid/ask like a real marketable order.

CLOCK
  09:15 -> 15:30 of the chosen day; play / pause / 1-2-5-10-30x / jump FORWARD to a time. Jumping
  back is not allowed: fills already made would be in the future of the clock.

ISOLATION
  Own paper desk, own folder (data/practice_sessions/<id>/), own control file. Nothing here can
  reach a broker, arm anything, or touch a live deployment.
"""
import datetime as dt
import json
import logging
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from live_trading_options.strategy_builder import config as C
from live_trading_options.strategy_builder.chain_feed import ChainFeed
from live_trading_options.strategy_builder import orders as O
from live_trading_options.strategy_builder import practice_data as PD

log = logging.getLogger("sb.practice")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SESS_DIR = C.DATA / "practice_sessions"
DAY_SECONDS = 375 * 60                     # 09:15:00 -> 15:30:00
SPEEDS = (1, 2, 5, 10, 30)
TICK = 0.05
# full bid-ask spread in rupees = a + b * premium  (NIFTY is very liquid; SENSEX a little less)
SPREAD = {"NIFTY": (0.10, 0.002), "SENSEX": (0.20, 0.004)}


def _tick(x):
    return round(round(x / TICK) * TICK, 2)


class Replay:
    """Price of every contract (and the index) at any second of the day."""

    def __init__(self, index: str, date: str):
        self.meta = PD.read_meta(index, date)
        if not self.meta:
            raise ValueError(f"no practice pack for {index} {date} — prepare the day first")
        self.index, self.date, self.expiry = index, date, self.meta["expiry"]
        d = PD.pack_dir(index, date)
        self.base = dt.datetime.fromisoformat(f"{date}T09:15:00").replace(tzinfo=IST)
        base_naive = pd.Timestamp(f"{date} 09:15:00")
        mins = pd.read_parquet(d / "min.parquet")
        self.contracts = {}
        for (k, typ), g in mins.groupby(["strike", "type"]):
            m = ((pd.to_datetime(g["t"]) - base_naive).dt.total_seconds() // 60).astype(int).to_numpy()
            ok = (m >= 0) & (m < 375)
            arr = np.full((375, 6), np.nan)
            arr[m[ok]] = g[["o", "h", "l", "c", "oi", "vol"]].to_numpy(float)[ok]
            self._ffill(arr)
            self.contracts[(float(k), typ)] = {"min": arr, "sec": None}
        sp = d / "sec.parquet"
        if sp.exists():
            sec = pd.read_parquet(sp)
            for (k, typ), g in sec.groupby(["strike", "type"]):
                c = self.contracts.get((float(k), typ))
                if c is None:
                    continue
                s = (pd.to_datetime(g["t"]) - base_naive).dt.total_seconds().to_numpy()
                o = np.argsort(s)
                c["sec"] = (s[o], g["c"].to_numpy(float)[o])
        spot = pd.read_parquet(d / "spot.parquet")
        m = ((pd.to_datetime(spot["t"]) - base_naive).dt.total_seconds() // 60).astype(int).to_numpy()
        ok = (m >= 0) & (m < 375)
        self.spot = np.full((375, 6), np.nan)
        self.spot[m[ok], :4] = spot[["o", "h", "l", "c"]].to_numpy(float)[ok]
        self._ffill(self.spot)
        self.day_open = float(self.meta.get("open") or self.spot[0, 0])

    @staticmethod
    def _ffill(arr):
        """A minute with no trade = flat at the previous close (o=h=l=c), OI carried."""
        last = None
        for i in range(len(arr)):
            if np.isnan(arr[i, 3]):
                if last is not None:
                    arr[i, :4] = last[3]
                    arr[i, 4] = last[4]
                    arr[i, 5] = 0
            else:
                last = arr[i].copy()

    @staticmethod
    def _path(bar, f):
        """Price inside a 1-minute bar at fraction f (0..1)."""
        o, h, l, c = bar[:4]
        if np.isnan(c):
            return None
        pts = (o, l, h, c) if c >= o else (o, h, l, c)
        x = min(max(f, 0.0), 1.0) * 3
        i = min(int(x), 2)
        return float(pts[i] + (pts[i + 1] - pts[i]) * (x - i))

    def price(self, key, s: float):
        c = self.contracts.get(key)
        if c is None:
            return None
        if c["sec"] is not None:
            ts, cl = c["sec"]
            j = int(np.searchsorted(ts, s, side="right")) - 1
            if j >= 0:
                return float(cl[j])
        m = int(min(max(s // 60, 0), 374))
        return self._path(c["min"][m], (s - m * 60) / 60.0)

    def oi(self, key, s):
        c = self.contracts.get(key)
        if c is None:
            return None, None
        m = int(min(max(s // 60, 0), 374))
        v = c["min"][m, 4]
        first = next((x for x in c["min"][:, 4] if not np.isnan(x)), None)
        return (None if np.isnan(v) else float(v)), (None if np.isnan(v) or first is None else float(v - first))

    def spot_at(self, s):
        m = int(min(max(s // 60, 0), 374))
        return self._path(self.spot[m], (s - m * 60) / 60.0)


class ReplayFeed(ChainFeed):
    """ChainFeed whose data and clock come from a Replay instead of Fyers."""

    def __init__(self, replay: Replay, start_s: float = 0.0):
        super().__init__(load_cache=False)
        self.r = replay
        self.s = float(start_s)
        self.playing = False
        self.speed = 1
        self.ended = False
        self._alive = True
        idx, exp = replay.index, replay.expiry
        self.sym = {}
        by_symbol, by_strike = {}, {}
        for (k, typ) in sorted(replay.contracts):
            sym = f"PX:{idx}:{exp}:{k:g}:{typ}"
            self.sym[sym] = (k, typ)
            row = {"symbol": sym, "strike": k, "type": typ, "ltp": None, "bid": None, "ask": None,
                   "oi": None, "oich": None, "volume": None, "chg": None, "f_iv": None, "f_delta": None}
            by_symbol[sym] = row
            by_strike.setdefault(k, {})[typ] = row
        self.chains[(idx, exp)] = {"by_symbol": by_symbol, "by_strike": by_strike, "fetched": time.time()}
        dte = (dt.date.fromisoformat(exp) - dt.date.fromisoformat(replay.date)).days
        self.expiries = {idx: [{"date": exp, "epoch": None, "flag": "W", "dte": dte}]}
        self._update()

    # ── no Fyers anywhere ──
    def _ensure_clients(self):
        pass

    def touch(self, index, expiry):
        pass

    def now(self) -> dt.datetime:
        return self.r.base + dt.timedelta(seconds=self.s)

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="SB-replay").start()

    def stop(self):
        self._alive = False

    def _run(self):
        last = time.monotonic()
        while self._alive:
            time.sleep(0.2)
            now = time.monotonic()
            dtr, last = now - last, now
            with self._lock:
                if self.playing and not self.ended:
                    self.s = min(DAY_SECONDS - 1, self.s + dtr * self.speed)
                    if self.s >= DAY_SECONDS - 1:
                        self.ended, self.playing = True, False
            self._update()

    def _update(self):
        r, s = self.r, self.s
        a, b = SPREAD[r.index]
        ch = self.chains[(r.index, r.expiry)]
        now = time.time()
        with self._lock:
            for sym, key in self.sym.items():
                ltp = r.price(key, s)
                row = ch["by_symbol"][sym]
                oi, oich = r.oi(key, s)
                row.update(oi=oi, oich=oich)
                if ltp is None:
                    continue
                half = max(TICK, _tick((a + b * ltp) / 2))
                self.live[sym] = {"ltp": _tick(ltp), "bid": max(TICK, _tick(ltp - half)),
                                  "ask": _tick(ltp + half), "t": now}
                row["ltp"] = _tick(ltp)
            sp = r.spot_at(s)
            if sp is not None:
                self.live[C.INDEX_SYMBOL[r.index]] = {"ltp": round(sp, 2), "t": now}
                self.under[r.index] = {"spot": round(sp, 2), "fut": None, "chg": sp - r.day_open,
                                       "chgp": 100 * (sp - r.day_open) / r.day_open,
                                       "symbol": C.INDEX_SYMBOL[r.index], "rest_at": now}
            ch["fetched"] = now
            self.last_tick_at = now

    # ── controls ──
    def control(self, action: str, value=None):
        with self._lock:
            if action == "play":
                if not self.ended:
                    self.playing = True
            elif action == "pause":
                self.playing = False
            elif action == "speed":
                v = int(value)
                if v not in SPEEDS:
                    raise ValueError(f"speed must be one of {SPEEDS}")
                self.speed = v
            elif action == "seek":
                h, m = [int(x) for x in str(value).split(":")[:2]]
                target = (h * 60 + m - (9 * 60 + 15)) * 60
                if target < self.s:
                    raise ValueError("can only jump FORWARD (your fills are already in the past)")
                self.s = float(min(target, DAY_SECONDS - 1))
            else:
                raise ValueError(f"unknown action {action}")
        self._update()

    def state(self) -> dict:
        n = self.now()
        return {"time": n.strftime("%H:%M:%S"), "playing": self.playing, "speed": self.speed,
                "ended": self.ended, "progress": round(self.s / DAY_SECONDS, 4)}

    def status(self) -> dict:
        return {"practice": True, "ws_connected": True, "subscribed": len(self.sym), "watched": [],
                "last_tick_age": 0, "errors": []}


class _PaperAcct:
    id, name, kind, parked, status, error, funds, day, client = "paper", "Paper", "paper", False, "logged_in", None, None, None, "paper"

    def __init__(self):
        self.lock = threading.RLock()

    def ensure(self):
        return "paper"

    def refresh_funds(self):
        pass

    def has_creds(self):
        return True

    def login(self):
        return True

    def public(self):
        return {"id": "paper", "name": "Paper", "kind": "paper", "status": "logged_in", "error": None,
                "funds": None, "since": None, "today": True, "parked": False}


class _PracticeHub:
    def __init__(self):
        self.accounts = {"paper": _PaperAcct()}

    def get(self, aid):
        if aid != "paper":
            raise KeyError("practice mode trades on Paper only")
        return self.accounts["paper"]

    def status(self):
        return [self.accounts["paper"].public()]


class PracticeManager:
    """One active practice session at a time; every session is saved and can be resumed."""

    def __init__(self):
        SESS_DIR.mkdir(parents=True, exist_ok=True)
        self.feed: ReplayFeed | None = None
        self.desk: O.Desk | None = None
        self.sess: dict | None = None
        self._mon_alive = False
        self._last_comb = 0.0
        self._lock = threading.RLock()

    @property
    def active(self) -> bool:
        return self.sess is not None

    # ── lifecycle ──
    def start(self, index: str, date: str) -> dict:
        if index not in C.INDICES:
            raise ValueError("index must be NIFTY or SENSEX")
        rp = Replay(index, date)
        name = f"Practice {index} {dt.date.fromisoformat(date):%d-%b-%Y}"
        names = {s["name"] for s in self.sessions()}
        base, n = name, 2
        while name in names:
            name, n = f"{base} #{n}", n + 1
        sid = uuid.uuid4().hex[:8]
        sess = {"id": sid, "name": name, "index": index, "date": date, "expiry": rp.expiry,
                "s": 0.0, "speed": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": rp.meta.get("source"), "seconds": len(rp.meta.get("seconds") or [])}
        return self._open(sess, rp)

    def resume(self, sid: str) -> dict:
        p = SESS_DIR / sid / "session.json"
        if not p.exists():
            raise ValueError("unknown practice session")
        sess = json.loads(p.read_text(encoding="utf-8"))
        return self._open(sess, Replay(sess["index"], sess["date"]))

    def _open(self, sess, rp) -> dict:
        with self._lock:
            self.end()
            folder = SESS_DIR / sess["id"]
            folder.mkdir(parents=True, exist_ok=True)
            feed = ReplayFeed(rp, sess.get("s", 0.0))
            feed.speed = sess.get("speed", 1)
            lot = rp.meta["lot"]

            def quote(sym):
                return feed.quotes([sym]).get(sym)

            def leg_quotes(legs):
                return feed.quotes([l["fyers_symbol"] for l in legs if l.get("fyers_symbol")])

            def resolve(acct, leg):
                return {"symbol": leg["fyers_symbol"], "exchange": "PAPER", "lot": lot, "token": None}

            desk = O.Desk(_PracticeHub(), leg_quotes, {"paper": O.PaperAdapter(quote, clock=feed.now)},
                          resolve, clock=feed.now, dep_dir=folder / "deployments",
                          control=folder / "control.json")
            self.feed, self.desk, self.sess = feed, desk, sess
            self._save()
            feed.start()
            self._mon_alive = True
            threading.Thread(target=self._monitor, daemon=True, name="SB-practice-mon").start()
            log.info("practice %s opened at %s", sess["name"], feed.state()["time"])
            return self.state()

    def end(self):
        with self._lock:
            if not self.sess:
                return
            self.feed.playing = False
            self._save()
            self._mon_alive = False
            self.feed.stop()
            log.info("practice %s closed at %s", self.sess["name"], self.feed.state()["time"])
            self.feed = self.desk = self.sess = None

    def _save(self):
        if not self.sess:
            return
        self.sess.update(s=self.feed.s, speed=self.feed.speed, clock=self.feed.state()["time"],
                         updated=time.strftime("%Y-%m-%d %H:%M:%S"))
        folder = SESS_DIR / self.sess["id"]
        try:
            views = self.desk.list(include_closed_days=10000) if self.desk else []
            mtms = [v["mtm"] for v in views]
            self.sess["mtm"] = None if any(m is None for m in mtms) else sum(mtms)
            self.sess["strategies"] = len(views)
        except Exception:
            pass
        tmp = folder / "session.tmp"
        tmp.write_text(json.dumps(self.sess, indent=1), encoding="utf-8")
        tmp.replace(folder / "session.json")

    def _monitor(self):
        last_save = time.monotonic()
        while self._mon_alive:
            try:
                self.desk.tick()
                self._combined()
                if time.monotonic() - last_save > 5:
                    self._save()
                    last_save = time.monotonic()
            except Exception as e:
                log.warning("practice monitor: %s", e)
            time.sleep(0.2)

    def _combined(self):
        """Whole-session MTM + CE/PE imbalance every 5 replay-seconds (the session chart)."""
        t = self.feed.now()
        if t.timestamp() - self._last_comb < O.HIST_EVERY or not self.feed.playing:
            return
        self._last_comb = t.timestamp()
        views = self.desk.list(include_closed_days=10000)
        if not views:
            return
        mtms = [v["mtm"] for v in views]
        ce = [v["short_ce"] for v in views]
        pe = [v["short_pe"] for v in views]
        mtm = None if any(m is None for m in mtms) else sum(mtms)
        cs = None if any(x is None for x in ce) else sum(ce)
        ps = None if any(x is None for x in pe) else sum(pe)
        imb = (100 * (cs - ps) / (cs + ps)) if cs is not None and ps is not None and (cs + ps) else None
        rec = {"t": t.strftime("%Y-%m-%d %H:%M:%S"), "mtm": mtm, "ce": cs, "pe": ps, "imb": imb,
               "open": sum(v["open_qty"] for v in views)}
        with open(SESS_DIR / self.sess["id"] / "combined.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    # ── reads ──
    def state(self) -> dict:
        if not self.sess:
            return {"active": False}
        return {"active": True, "session": {k: self.sess.get(k) for k in
                                            ("id", "name", "index", "date", "expiry", "source", "seconds")},
                **self.feed.state(), "speeds": list(SPEEDS)}

    def combined(self, sid: str) -> list:
        p = SESS_DIR / sid / "combined.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def sessions(self) -> list:
        out = []
        for p in sorted(SESS_DIR.glob("*/session.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        return sorted(out, key=lambda s: s.get("updated", ""), reverse=True)
