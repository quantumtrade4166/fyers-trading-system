r"""
replays/sim.py — LIVE-LIKE SIMULATOR for the Synthetic / Premium books (owner 27-Nov-2026).
=========================================================================================

"The recorded data flows through like a live market, trading on the selected settings - I can increase /
decrease lots, change strike gaps just like the live market."

One session at a time, run inside replay_server.py on its own thread:
  * a recorded day (data/quotes/{date}/{und}.qrec) is fed tick by tick from any start time, at a chosen
    speed (1x ... 600x, pause), through the REAL strategy code (SynToFro / SynToFroV2) on a PaperBook
  * the fake clock (replay_rules.Clock) follows the tick time, so entry / exit time / MTM stop rules see
    recording time
  * controls (lots, strike gap, strikes, Start OTM, MTM stop, target, exit time, Enter now, Exit all)
    are queued and applied by the sim thread at the current recording moment - the same code path the
    live engine uses (replay_syn.apply_change -> strategy.params_changed / resize)
  * frames (same shape as the replay player's timeline) are appended as the day plays; the page polls
"""
import copy
import copyreg
import datetime as dt
import queue
import threading
import time as _wall

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import engine as EN
from live_trading_options.cepe_imbalance import quotes as Q
from live_trading_options.cepe_imbalance import replay_rules as RR
from live_trading_options.cepe_imbalance import replay_syn as R
from live_trading_options.cepe_imbalance import synthetic as SY
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.market import Market

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CLOSE = dt.time(15, 30)
WARM_S = 900                     # ticks before the start time, applied at once (prices for every strike)
FRAME_EVERY = 1.0                # recording seconds between chart frames (x speed/60 when fast)
EVENTS = ("adjust", "params", "reshape_skipped", "exit_time", "stop_loss", "profit_exit", "level_exit", "stop",
          "blocked", "entry_recentre")
CONTROL = {"lots": int, "strike_gap": float, "strikes": int, "start_otm": int, "mtm_stop": float,
           "profit_target": float, "exit_time": str, "buffer": float}


# rewind = restore a saved copy of the whole book + market; the runner holds locks, which copy as new locks
copyreg.pickle(type(threading.RLock()), lambda _l: (threading.RLock, ()))
copyreg.pickle(type(threading.Lock()), lambda _l: (threading.Lock, ()))
CKPT_EVERY = 120.0               # recording seconds between saved copies (a rewind replays at most this much)


def hms(t):
    return dt.datetime.fromtimestamp(t, IST).strftime("%H:%M:%S")


class Sim:
    def __init__(self, und, date, rules="syn2", lots=1, params=None, start="09:15:00", entry="09:20:00",
                 exit_="15:25:00"):
        self.und, self.date, self.rules = und, date, rules
        day = dt.date.fromisoformat(date)
        at = lambda s: dt.datetime.combine(day, dt.time.fromisoformat(s), IST).timestamp()
        self.t_start, self.t_entry, self.t_close = at(start), at(entry), dt.datetime.combine(day, CLOSE, IST).timestamp()
        self.t = self.t_start
        self.speed, self.playing, self.done, self.error = 1.0, False, False, ""
        self.frames, self.cmds, self.lock = [], queue.Queue(), threading.Lock()
        self.entered = self.t_entry <= self.t_start - 1e9      # entry time handled in the loop
        self.armed = True                                     # entry fires at the entry time (or Enter now)
        self.started = False
        self._stop = threading.Event()
        self.seq = 0
        self.jump_to = None                                   # fast-forward target (player bar dragged ahead)
        self.ckpts = []                                       # [(t, copy of the whole state)] for rewinds
        self.epoch = 0                                        # +1 on every rewind: the page redraws from scratch
        self.rewinding = False
        import uuid
        self.sid = uuid.uuid4().hex[:12]                     # only the window that started it controls it

        RR.install_clock()
        SY.time = RR._fake_time
        RR.Clock.t = self.t_start - WARM_S - 600
        self.u = R.universe_from_recording(und, date)
        self.m = Market(self.u)
        r = EN.Runner(und + C.SYN_SUFFIX, self.m, persist=False, date=date)
        if rules == "syn2":
            r.strategy, r.strategy_name = SY.SynToFroV2(), "syn_tofro_v2"
            r.params = dict(SY.SynToFroV2.params)
        r.ctx.in_session = lambda now=None: True
        r.params.update(lots=int(lots), exit_time=exit_)
        for k, v in (params or {}).items():
            if k in r.strategy.params:
                r.params[k] = CONTROL.get(k, type(r.strategy.params[k]))(v)
        self.r = r
        self.lot = self.u.underlyings[und].lot
        self.it = Q.iter_window(date, und, self.t_start - WARM_S, self.t_close + 1)
        self.pending = None
        self.next_eval = 0.0
        self.last_frame = 0.0
        self._warm()
        self.th = threading.Thread(target=self._loop, name="sim", daemon=True)
        self.th.start()

    # ── feed ───────────────────────────────────────────────────────────────
    def _next(self):
        if self.pending is not None:
            row, self.pending = self.pending, None
            return row
        try:
            return next(self.it)[1]
        except StopIteration:
            return None

    def _apply(self, row):
        RR.Clock.t = row[0]
        self.m.apply([Tick(row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], int(row[0]), row[0])])

    def _warm(self):
        while True:
            row = self._next()
            if row is None:
                break
            if row[0] >= self.t_start:
                self.pending = row
                break
            self._apply(row)
        RR.Clock.t = self.t_start
        self._frame(force=True)
        self._checkpoint()

    # ── rewind ─────────────────────────────────────────────────────────────
    def _checkpoint(self):
        """Save the whole state at self.t (taken between batches: every row <= self.t is applied)."""
        blob = copy.deepcopy((self.r, self.m, self.next_eval, self.started, self.armed))
        while self.ckpts and self.ckpts[-1][0] >= self.t:
            self.ckpts.pop()
        self.ckpts.append((self.t, blob))

    def _rewind(self, t):
        """Back to recording time t: restore the last copy at or before t, then replay the ticks up to t."""
        t = max(self.t_start, min(t, self.t))
        i = max(k for k, c in enumerate(self.ckpts) if c[0] <= t)
        ct, blob = self.ckpts[i]
        del self.ckpts[i + 1:]
        self.r, self.m, self.next_eval, self.started, self.armed = copy.deepcopy(blob)
        self.t = ct
        RR.Clock.t = ct
        self.it = Q.iter_window(self.date, self.und, ct + 1e-6, self.t_close + 1)
        self.pending = None
        self.done = False
        with self.lock:
            self.frames = [f for f in self.frames if f["t"] <= ct]
        self.last_frame = self.frames[-1]["t"] if self.frames else 0.0
        self.playing = False                                  # rewound: paused, press Play to go on
        self.jump_to = t if t > ct else None
        self.rewinding = self.jump_to is not None
        self.epoch += 1

    # ── strategy ───────────────────────────────────────────────────────────
    def _evaluate(self, now):
        r, st = self.r, self.r.strategy
        RR.Clock.t = now
        ph = r.state.get("phase")
        if not self.started:
            if self.armed and now >= self.t_entry:
                r.running = True
                self.started = True
                st.on_start(r.ctx)
            return
        if ph == "wait_entry":
            st.on_start(r.ctx)
        elif ph == "in":
            st.on_tick(r.ctx, [])

    def _frame(self, force=False):
        now = RR.Clock.t
        far = self.jump_to is not None and not self.rewinding
        every = FRAME_EVERY * max(1.0, self.speed / 60, 10.0 if far else 0)
        if not force and now - self.last_frame < every:
            return
        self.last_frame = now
        f = R.snapshot(self.r, self.m, self.und, now)
        with self.lock:
            self.frames.append(f)

    # ── controls (run on the sim thread) ───────────────────────────────────
    def _command(self, c):
        r = self.r
        kind = c.get("cmd")
        if kind == "play":
            self.playing = True
        elif kind == "pause":
            self.playing = False
            self.jump_to = None                               # pause stops a fast-forward too
        elif kind == "seek":
            t = float(c.get("t") or 0)
            if t > self.t:
                self.jump_to = min(t, self.t_close)
            elif t < self.t - 0.5:
                self._rewind(t)
                return
        elif kind == "speed":
            self.speed = max(0.25, min(3000.0, float(c.get("v") or 1)))
        elif kind == "set":
            ch = {}
            for k, v in (c.get("set") or {}).items():
                if k in CONTROL and k in r.strategy.params and v not in ("", None):
                    ch[k] = CONTROL[k](v)
            if "lots" in ch and not 1 <= ch["lots"] <= 50:
                return
            if ch:
                R.apply_change(r, ch)
                if r.state.get("resize_to") and r.state.get("phase") == "in":
                    r.strategy.evaluate(r.ctx)                # resize at once, like the live worker
        elif kind == "enter":
            if any(p.lots for p in r.book.positions.values()):
                return
            r.state["phase"] = "wait_entry"
            r.state["entry_left"] = None
            self.armed, self.started = True, True
            r.running = True
            r.strategy.on_start(r.ctx)
            r.event("enter_now")
        elif kind == "exit":
            out = r.book.exit_all("exit all (button)")
            r.state["phase"] = "exited"
            r.state["blocked"] = "exited by the Exit all button - Enter now to trade again"
            r.event("stop", exited=len(out))
        self._frame(force=True)
        if kind in ("set", "enter", "exit"):
            self._checkpoint()                                # a rewind past this point keeps the change

    def send(self, c):
        self.cmds.put(c)

    def close(self):
        self._stop.set()

    # ── loop ───────────────────────────────────────────────────────────────
    def _loop(self):
        last = _wall.monotonic()
        try:
            while not self._stop.is_set():
                while not self.cmds.empty():
                    self._command(self.cmds.get_nowait())
                now_w = _wall.monotonic()
                dt_w, last = now_w - last, now_w
                jumping = self.jump_to is not None and not self.done
                if (not self.playing and not jumping) or self.done:
                    _wall.sleep(0.05)
                    continue
                target = min(self.t_close, self.jump_to if jumping else self.t + dt_w * self.speed)
                budget = now_w + 0.25                         # keep the controls responsive at high speed
                while True:
                    row = self._next()
                    if row is None:
                        self.done = True
                        break
                    if row[0] > target:
                        self.pending = row
                        break
                    if row[0] >= self.next_eval:              # evaluate once per 0.2 s of recording
                        self._evaluate(row[0])
                        self.next_eval = row[0] + RR.EVAL_EVERY_S
                        self._frame()
                    self._apply(row)
                    if _wall.monotonic() > budget:
                        target = row[0]                       # fell behind: show the real (slower) speed
                        break
                self.t = target
                RR.Clock.t = target
                if jumping and self.t >= self.jump_to:
                    self.jump_to = None
                    self.rewinding = False
                    self._frame(force=True)
                if self.t - self.ckpts[-1][0] >= CKPT_EVERY:
                    self._checkpoint()
                if self.t >= self.t_close:
                    self.done = True
                if self.done:
                    self._evaluate(self.t)
                    self._frame(force=True)
                _wall.sleep(0.02)
        except Exception as e:                                # surfaced on the page
            import traceback
            self.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            self.playing = False

    # ── read model ─────────────────────────────────────────────────────────
    def state(self, since=0):
        r = self.r
        with self.lock:
            frames = self.frames[since:]
            n = len(self.frames)
        mt = r.book.mtm()
        try:
            ch = R.charges(r.book.trades, self.lot, self.date)
        except Exception:
            ch = None
        legs = SY.SynToFro.legs(r.ctx)
        p = r.params
        return {
            "sid": self.sid, "und": self.und, "date": self.date, "rules": self.rules, "lot": self.lot, "expiry": r.ctx.expiry,
            "t": self.t, "clock": hms(self.t), "t_start": self.t_start, "t_close": self.t_close,
            "jumping": self.jump_to is not None, "rewinding": self.rewinding, "epoch": self.epoch, "speed": self.speed, "playing": self.playing,
            "done": self.done, "error": self.error, "phase": r.state.get("phase") or ("waiting for entry time" if not self.started else ""),
            "blocked": r.state.get("blocked"), "entry_at": hms(self.t_entry), "started": self.started,
            "pending": bool(r.state.get("resize_to")),
            "params": {k: p.get(k) for k in ("lots", "strike_gap", "strikes", "start_otm", "mtm_stop", "profit_target", "exit_time") if k in p},
            "shape": r.state.get("shape"), "held": {k: len(v) for k, v in legs.items()},
            "mtm": round(mt["total"], 2), "gross": round(mt["total"], 2), "charges": ch,
            "net": round(mt["total"] - (ch or 0), 2), "orders": len(r.book.trades),
            "adjustments": sum(1 for e in r.events if e.get("event") == "adjust" and not str(e.get("action", "")).startswith(("entry", "reshape"))),
            "n": n, "since": since, "frames": frames,
            "events": [e for e in r.events if e.get("event") in EVENTS],
        }


SIM = None
_LOCK = threading.Lock()


def start(**kw):
    global SIM
    with _LOCK:
        if SIM:
            SIM.close()
            SIM.th.join(timeout=3)
        SIM = Sim(**kw)
        return SIM
