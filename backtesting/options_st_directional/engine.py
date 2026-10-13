"""One trading day of the strategy, as an explicit state machine.

Two states never overwrite each other:
  * position -- the open short option (at most one)
  * setup    -- the latest directional setup: contract, evolving reference low,
                and (once it fires) a pending entry signal

Clock: candle `t` (labelled at its open) is complete at T = t + 5 min. Every
decision at T uses candles up to and including `t` only. Order at each T:

  1. exit check of the open position on its own option candle t
  2. 15:15 square-off (pure intraday) -- nothing else happens at/after it
  3. spot Supertrend flip on spot candle t -> old setup (and any pending signal)
     discarded, new setup created; its reference low = the new option's candle t
  4. otherwise the existing setup evaluates option candle t (created before T only)
  5. entry if flat and the latest setup holds a signal -> fill at T

The data source is injected (`provider`) so the same machine runs on synthetic
fixtures in the tests, on Breeze data in the backtest (run_day), and candle by
candle on live ticks in the paper engine (DayMachine.on_close / square_off).
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

BULL, BEAR = 1, -1


@dataclass
class Setup:
    setup_id: str
    origin: str                      # "initial" | "reversal"
    direction: int                   # BULL -> sell PE, BEAR -> sell CE
    opt_type: str
    created_at: pd.Timestamp         # time the setup became known (a candle close)
    selection_ts: pd.Timestamp
    spot_at_selection: float
    strike: int
    expiry: pd.Timestamp
    expiry_reason: str
    ref_candle_ts: pd.Timestamp | None = None
    ref_low_initial: float | None = None
    ref_low: float | None = None
    ref_updates: list = field(default_factory=list)   # [(candle_ts, old, new)]
    status: str = "watching"         # watching|signalled|executed|invalidated|expired_eod|no_data
    signal_candle_ts: pd.Timestamp | None = None
    signal_ts: pd.Timestamp | None = None             # candle close = signal time
    signal_close: float | None = None
    signal_ref: float | None = None
    signal_opt_st_dir: int | None = None
    pending_while_open: bool = False
    invalidated_at: pd.Timestamp | None = None
    notes: list = field(default_factory=list)

    @property
    def contract(self):
        return (self.expiry, self.strike, self.opt_type)


def _ts(day: str, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{day} {hhmm}")


class DayMachine:
    """The strategy for one session. Feed it each completed 5-min candle `t` in
    order (on_close), then square_off() once. The backtest and the live paper
    engine share this class, so they cannot drift apart."""

    def __init__(self, day: str, provider, cfg):
        self.day, self.provider, self.cfg = day, provider, cfg
        self.step = pd.Timedelta(minutes=cfg.timeframe_min)
        self.first = _ts(day, cfg.first_candle)
        self.sq = _ts(day, cfg.square_off)
        self.events, self.setups, self.trades, self.reversals = [], [], [], []
        self.seq = {"setup": 0, "trade": 0}
        self.position, self.setup, self.bias = None, None, None
        self.last_closed = None
        self.done = False

    # ---------- helpers ----------
    def log(self, ts, event, setup=None, trade=None, **detail):
        self.events.append({"date": self.day, "ts": ts, "event": event,
                            "setup_id": setup.setup_id if setup else None,
                            "trade_id": trade["trade_id"] if trade else None,
                            "detail": "; ".join(f"{k}={v}" for k, v in detail.items())})

    def option_candle(self, contract, t):
        oc = self.provider.option_candles(self.day, contract)
        if oc is None or oc.empty or t not in oc.index:
            return None
        return oc.loc[t]

    def new_setup(self, direction, T, t, spot_close, origin):
        self.seq["setup"] += 1
        sel = self.provider.select_contract(self.day, T, spot_close)
        s = Setup(setup_id=f"{self.day}-S{self.seq['setup']}", origin=origin,
                  direction=direction, opt_type="PE" if direction == BULL else "CE",
                  created_at=T, selection_ts=T, spot_at_selection=float(spot_close),
                  strike=sel["strike"], expiry=pd.Timestamp(sel["expiry"]),
                  expiry_reason=sel["reason"])
        self.setups.append(s)
        self.log(T, "setup_created", s, origin=origin,
                 bias="bull" if direction == BULL else "bear",
                 contract=f"{s.strike}{s.opt_type} exp {s.expiry.date()}",
                 spot=round(s.spot_at_selection, 2), expiry_reason=s.expiry_reason)
        oc = self.provider.option_candles(self.day, s.contract)
        if oc is None or oc.empty:
            s.status = "no_data"
            s.notes.append("no option candles for selected contract")
            self.log(T, "setup_no_data", s)
            return s
        c = self.option_candle(s.contract, t)
        if c is None:
            s.notes.append(f"reference candle {t.time()} missing; first later candle used")
            self.log(T, "reference_candle_missing", s, candle=t.time())
        else:
            s.ref_candle_ts = t
            s.ref_low_initial = s.ref_low = float(c["low"])
            self.log(T, "reference_set", s, candle=t.time(), ref_low=s.ref_low)
        return s

    def evaluate(self, s, t, T):
        c = self.option_candle(s.contract, t)
        if c is None:
            s.notes.append(f"candle {t.time()} missing")
            self.log(T, "option_candle_missing", s, candle=t.time())
            return
        low, close = float(c["low"]), float(c["close"])
        if s.ref_low is None:                      # reference candle was missing
            s.ref_candle_ts = t
            s.ref_low_initial = s.ref_low = low
            self.log(T, "reference_set", s, candle=t.time(), ref_low=low, late=True)
            return
        if close < s.ref_low:                      # Case A
            s.status = "signalled"
            s.signal_candle_ts, s.signal_ts = t, T
            s.signal_close, s.signal_ref = close, s.ref_low
            d = c.get("st_dir", np.nan)
            s.signal_opt_st_dir = None if pd.isna(d) else int(d)
            self.log(T, "entry_signal", s, candle=t.time(), close=close, ref_low=s.ref_low)
        elif low < s.ref_low:                      # Case B
            s.ref_updates.append((t, s.ref_low, low))
            self.log(T, "reference_lowered", s, candle=t.time(), old=s.ref_low, new=low,
                     close=close)
            s.ref_low = low
        # Case C: nothing

    def open_trade(self, s, T, fill_ts, price):
        self.seq["trade"] += 1
        tr = {
            "trade_id": f"{self.day}-T{self.seq['trade']}", "date": self.day,
            "setup_id": s.setup_id, "origin": s.origin,
            "bias": "bull" if s.direction == BULL else "bear",
            "opt_type": s.opt_type, "strike": s.strike, "expiry": s.expiry.date(),
            "expiry_reason": s.expiry_reason,
            "spot_at_selection": s.spot_at_selection, "selection_ts": s.selection_ts,
            "ref_candle_ts": s.ref_candle_ts, "ref_low_initial": s.ref_low_initial,
            "ref_updates": " | ".join(f"{a.time()}:{o}->{n}" for a, o, n in s.ref_updates),
            "signal_ts": s.signal_ts, "signal_close": s.signal_close,
            "signal_ref": s.signal_ref, "signal_opt_st_dir": s.signal_opt_st_dir,
            "pending_while_open": s.pending_while_open,
            "prev_position_closed_ts": self.last_closed,
            "earliest_exec_ts": T,
            "entry_ts": fill_ts, "entry_price": price,
            "exit_signal_ts": None, "exit_ts": None, "exit_price": None,
            "exit_reason": None, "exit_close": None, "exit_st": None,
            "anomalies": "", "contract": s.contract, "be_armed_ts": None,
        }
        s.status = "executed"
        self.trades.append(tr)
        self.log(fill_ts, "entry_fill", s, tr, price=price, signal_ts=s.signal_ts.time())
        return tr

    def close_trade(self, tr, fill_ts, price, reason, T, close=None, st=None, note=""):
        tr.update(exit_signal_ts=T, exit_ts=fill_ts, exit_price=price,
                  exit_reason=reason, exit_close=close, exit_st=st)
        if note:
            tr["anomalies"] = (tr["anomalies"] + "; " + note).strip("; ")
        self.log(fill_ts, "exit_fill", None, tr, price=price, reason=reason)

    # ---------- optional breakeven stop ----------
    def breakeven_check(self, t, T):
        """Walk the open position's 1-minute bars inside candle t (minutes t..T)."""
        pos, x = self.position, getattr(self.cfg, "breakeven_after_points", None)
        if pos is None or not x:
            return
        bars = self.provider.minute_bars(self.day, pos["contract"], t, T)
        if bars is None or bars.empty:
            return
        entry = float(pos["entry_price"])
        for r in bars.itertuples():
            m = pd.Timestamp(r.datetime)
            if m < pd.Timestamp(pos["entry_ts"]).floor("min"):
                continue
            if pos.get("be_armed_ts") is not None:
                if float(r.high) >= entry:
                    px = max(entry, float(r.open))          # gapped past the stop
                    self.log(m, "breakeven_stop", None, pos, minute=m.time(), stop=entry,
                             fill=px, armed=pos["be_armed_ts"].time())
                    self.close_trade(pos, m, px, "breakeven_stop", m)
                    self.last_closed = m
                    self.position = None
                    return
            elif float(r.low) <= entry - x:
                pos["be_armed_ts"] = m                       # active from the NEXT minute
                self.log(m, "breakeven_armed", None, pos, minute=m.time(),
                         low=float(r.low), trigger=round(entry - x, 2))

    # ---------- the clock ----------
    def on_close(self, t) -> bool:
        """Process completed candle t (decisions at T = t + 5 min).
        Returns False once the session is over (T reached square-off)."""
        if self.done:
            return False
        t = pd.Timestamp(t)
        T = t + self.step
        if t < self.first:
            return True
        self.breakeven_check(t, T)
        if T >= self.sq:
            # the 15:10 candle closes at 15:15: flat, nothing new
            self.square_off()
            return False
        prov = self.provider
        spot = prov.spot_candles(self.day)
        if spot is None or spot.empty or t not in spot.index:
            self.log(T, "spot_candle_missing", candle=t.time())
            return True
        srow = spot.loc[t]
        d = int(srow["st_dir"]) if not pd.isna(srow["st_dir"]) else 0

        if self.bias is None:                      # initial bias (09:15 candle)
            if d == 0:
                self.log(T, "spot_st_not_ready", candle=t.time())
                return True
            self.bias = d
            self.log(T, "initial_bias", bias="bull" if d == BULL else "bear",
                     spot_st=round(float(srow["st"]), 2), spot_close=float(srow["close"]))
            self.setup = self.new_setup(d, T, t, srow["close"], "initial")
            return True

        position = self.position
        # 1. exit check on the position's own option candle t
        exit_sig = None
        if position is not None:
            c = self.option_candle(position["contract"], t)
            if c is None:
                position["anomalies"] += f"; exit-check candle {t.time()} missing"
                self.log(T, "option_candle_missing", None, position, candle=t.time())
            elif pd.isna(c["st"]):
                self.log(T, "option_st_not_ready", None, position, candle=t.time())
            elif float(c["close"]) > float(c["st"]):
                exit_sig = (float(c["close"]), float(c["st"]))
                self.log(T, "exit_signal", None, position, candle=t.time(),
                         close=exit_sig[0], st=round(exit_sig[1], 2))

        if exit_sig is not None:
            f = prov.fill(self.day, position["contract"], T)
            if f is None:
                position["anomalies"] += f"; exit fill unavailable at {T.time()}"
                self.log(T, "exit_fill_unavailable", None, position)
            else:
                self.close_trade(position, f[0], f[1], "option_st", T, *exit_sig)
                self.last_closed = f[0]
                self.position = position = None

        # 3. spot flip
        if d != 0 and d != self.bias:
            self.reversals.append({"date": self.day, "ts": T, "candle": t,
                                   "from": "bull" if self.bias == BULL else "bear",
                                   "to": "bull" if d == BULL else "bear",
                                   "spot_close": float(srow["close"])})
            self.log(T, "spot_reversal", candle=t.time(),
                     to="bull" if d == BULL else "bear", spot_close=float(srow["close"]),
                     spot_st=round(float(srow["st"]), 2))
            if self.setup is not None and self.setup.status in ("watching", "signalled"):
                was_pending = self.setup.status == "signalled"
                self.setup.status, self.setup.invalidated_at = "invalidated", T
                self.log(T, "setup_invalidated", self.setup, had_pending_signal=was_pending)
            self.bias = d
            self.setup = self.new_setup(d, T, t, srow["close"], "reversal")

        # 4. evaluate the latest setup on candle t (only if it existed before T)
        elif (self.setup is not None and self.setup.status == "watching"
              and self.setup.created_at < T):
            self.evaluate(self.setup, t, T)

        # 5. entry
        s = self.setup
        if s is not None and s.status == "signalled":
            if self.position is None:
                f = prov.fill(self.day, s.contract, T)
                if f is None:
                    s.notes.append(f"entry fill unavailable at {T.time()}")
                    self.log(T, "entry_fill_unavailable", s)
                else:
                    self.position = self.open_trade(s, T, f[0], f[1])
            elif not s.pending_while_open:
                s.pending_while_open = True
                self.log(T, "signal_pending", s, open_trade=self.position["trade_id"])
        return True

    def square_off(self):
        """15:15: close any position, expire the setup. Idempotent."""
        if self.done:
            return
        self.done = True
        sq, prov = self.sq, self.provider
        if self.position is not None:
            pos = self.position
            f = prov.fill(self.day, pos["contract"], sq)
            note = ""
            if f is None:
                f = prov.last_price(self.day, pos["contract"], sq)
                note = "square-off fill missing; last traded close before 15:15 used"
            if f is None:
                pos["anomalies"] += "; NO square-off price available"
                self.log(sq, "square_off_unpriced", None, pos)
            else:
                self.close_trade(pos, f[0], f[1], "square_off_1515", sq, note=note)
                self.position = None
        s = self.setup
        if s is not None and s.status in ("watching", "signalled"):
            if s.status == "signalled":
                self.log(sq, "pending_signal_expired_eod", s)
            s.status = "expired_eod"

    def result(self) -> dict:
        trades = []
        for tr in self.trades:
            tr = {k: v for k, v in tr.items() if k != "contract"}
            tr["anomalies"] = tr["anomalies"].strip("; ")
            trades.append(tr)
        return {"trades": trades, "events": self.events, "setups": self.setups,
                "reversals": self.reversals}


def run_day(day: str, provider, cfg) -> dict:
    m = DayMachine(day, provider, cfg)
    spot = provider.spot_candles(day)
    if spot is None or spot.empty or m.first not in spot.index:
        m.log(m.first, "no_spot_data")
        m.done = True
        return m.result()
    for t in spot.index:
        if t + m.step > m.sq or not m.on_close(t):
            break
    m.square_off()
    return m.result()
