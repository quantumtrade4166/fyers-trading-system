"""
engine.py — Nifty ST Directional, live PAPER engine.
====================================================

Spot 5-min Supertrend(10,3) bias -> sell the ATM PE (bull) / CE (bear) when the
option's 5-min candle closes strictly below its evolving reference low -> buy back
when the option's own 5-min candle closes above its Supertrend(10,3), or 15:15.

EVERY decision is made by backtesting/options_st_directional/engine.py:DayMachine,
the exact class the backtest runs (52 tests; sim_test.py replays real days through
this engine and compares with the backtest). This file only:

    Kite ticks ─► 1-min OHLC per instrument ─► 5-min candles + Supertrend
               ─► DayMachine.on_close(t) at every 5-min boundary (+1.5 s)
    paper fills = option LTP at that moment

Market data: its own Kite ticker (never a Fyers socket — the VWAP strangle owns
that). Fyers REST history only for indicator warm-up (previous sessions + today's
minutes before the engine started). Paper only: it places no orders.

Restart-safe: the machine is snapshotted after every boundary. A restarted engine
reloads it and replays any boundary it missed from history candles, filling at the
historical 1-min open (= the backtest's fill rule), flagged as a catch-up fill.

Published (read-only consumer: deployment/stdir_api.py):
    data/live_state/TICK.json           ~1 s    spot, position, live MTM
    data/live_state/{date}_STATE.json   ~3 s    candles, Supertrend, reference low, trades, log
    data/live_state/{date}_MACHINE.pkl  per bar restart snapshot
    data/results/daily.json             per-day summary -> equity curve
    data/results/trades.jsonl           every closed trade
    data/results/{date}_mtm.jsonl       MTM every 5 s -> MTM chart

Run (VPS venv):  .venv\\Scripts\\python.exe -u live_trading_options\\nifty_st_directional\\engine.py
Single-instance lock: 127.0.0.1:47668.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
import time
import pickle
import calendar
import threading
import importlib.util
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO))

from backtesting.options_st_directional.engine import DayMachine          # noqa: E402
from backtesting.options_st_directional.config import Config              # noqa: E402
from backtesting.options_st_directional.candles import (  # noqa: E402
    to_5min, supertrend, clean_index_closing_auction)
from backtesting.options_pivot_intraday.costs import statutory_costs      # noqa: E402

PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text(encoding="utf-8"))
STATE_DIR = ROOT / "data" / "live_state"
RESULTS = ROOT / "data" / "results"
for d in (STATE_DIR, RESULTS):
    d.mkdir(parents=True, exist_ok=True)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PORT_LOCK = 47668
MKT_OPEN, MKT_CLOSE = dt.time(9, 15), dt.time(15, 30)
GRACE = 1.5                 # seconds after a boundary before the bar is processed
SPOT_SYMBOL_FYERS = "NSE:NIFTY50-INDEX"


def _load(rel: str, name: str):
    """Load a shared module by explicit path: several strategy packages have a
    `core/` or `live/` subpackage, and a plain import could resolve to another's."""
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def epoch(ts) -> int:
    """IST wall time as a UTC epoch (lightweight-charts has no timezone option)."""
    return calendar.timegm(pd.Timestamp(ts).timetuple())


def write_json(path: Path, obj) -> bool:
    """Atomic write that never raises (Windows os.replace fails while a reader holds
    the file; retry, then fall back to a direct write)."""
    data = json.dumps(obj, default=str)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
    except Exception as e:
        print(f"  [stdir] write failed {path.name}: {e}", flush=True)
        return False
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(0.05)
        except Exception:
            break
    try:
        path.write_text(data, encoding="utf-8")
        return True
    except Exception as e:
        print(f"  [stdir] direct write failed {path.name}: {e}", flush=True)
        return False


def append_jsonl(path: Path, obj) -> bool:
    line = json.dumps(obj, default=str) + "\n"
    for _ in range(20):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            return True
        except PermissionError:
            time.sleep(0.05)
        except Exception as e:
            print(f"  [stdir] append failed {path.name}: {e}", flush=True)
            return False
    return False


def _r(v, dp=2):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), dp)


# ════════════════════════════════════════════════════════════════════════════
class MinuteBook:
    """1-minute OHLC for one instrument from two sources:
       history (REST) for minutes before the first FULL tick minute, ticks after.
    The first tick minute counts as full only if the subscription was already live
    when that minute began (pre-open start); otherwise it is partial and the
    history bar is used for it."""

    def __init__(self, sub_at=None):
        self.ticks: dict = {}          # minute -> [o, h, l, c]
        self.hist: dict = {}
        self.sub_at = sub_at           # when the subscription went live
        self.full_from = None          # first minute fully covered by ticks

    def add_tick(self, ts: dt.datetime, px: float):
        m = pd.Timestamp(ts).floor("min")
        if self.full_from is None:
            whole = self.sub_at is not None and pd.Timestamp(self.sub_at) <= m
            self.full_from = m if whole else m + pd.Timedelta(minutes=1)
        b = self.ticks.get(m)
        if b is None:
            self.ticks[m] = [px, px, px, px]
        else:
            b[1] = max(b[1], px); b[2] = min(b[2], px); b[3] = px

    def add_history(self, df: pd.DataFrame):
        for r in df.itertuples():
            self.hist[pd.Timestamp(r.datetime)] = [r.open, r.high, r.low, r.close]

    def frame(self, until: pd.Timestamp) -> pd.DataFrame:
        """Complete minutes only (minute m complete when m + 1 min <= until)."""
        full_from = self.full_from
        rows = {}
        for m, b in self.hist.items():
            if full_from is None or m < full_from:
                rows[m] = b
        if full_from is not None:
            for m, b in list(self.ticks.items()):
                if m >= full_from:
                    rows[m] = b
        keys = sorted(k for k in rows if k + pd.Timedelta(minutes=1) <= until)
        if not keys:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])
        a = np.array([rows[k] for k in keys], dtype=float)
        return pd.DataFrame({"datetime": keys, "open": a[:, 0], "high": a[:, 1],
                             "low": a[:, 2], "close": a[:, 3]})

    def open_at(self, m: pd.Timestamp):
        b = self.ticks.get(m) if (self.full_from is not None and m >= self.full_from) else None
        b = b or self.hist.get(m)
        return None if b is None else b[0]

    def last_close_before(self, ts: pd.Timestamp):
        best = None
        for src in (self.hist, self.ticks):
            for m, b in src.items():
                if m < ts and (best is None or m > best[0]):
                    best = (m, b[3])
        return best


# ════════════════════════════════════════════════════════════════════════════
class LiveProvider:
    """The DayMachine's data interface, backed by the engine's live books."""

    def __init__(self, eng):
        self.e = eng

    def _until(self):
        return pd.Timestamp(self.e.clock()) - pd.Timedelta(seconds=GRACE - 0.5)

    def spot_candles(self, day):
        e = self.e
        with e.lock:
            f = e.spot_book.frame(self._until())
        # TradingView convention: no index candles after 15:10 on auction days
        f = clean_index_closing_auction(f) if not f.empty else f
        c5 = supertrend(to_5min(f), e.cfg.st_period, e.cfg.st_mult)
        if c5.empty:
            return c5
        c5 = c5[c5.index.strftime("%Y-%m-%d") == day]
        return c5[c5.index + pd.Timedelta(minutes=5) <= self._until()]

    def option_candles(self, day, contract):
        e = self.e
        key = e.contract_key(contract)
        if key not in e.instr:
            return pd.DataFrame()
        e.ensure_tracked(key)
        with e.lock:
            book = e.books.get(e.instr[key]["token"])
            f = book.frame(self._until()) if book else pd.DataFrame()
        if f.empty:
            return pd.DataFrame()
        c5 = supertrend(to_5min(f), e.cfg.st_period, e.cfg.st_mult)
        c5 = c5[c5.index.strftime("%Y-%m-%d") == day]
        return c5[c5.index + pd.Timedelta(minutes=5) <= self._until()]

    def select_contract(self, day, ts, spot):
        return self.e.select_contract(spot)

    def fill(self, day, contract, ts):
        """Live: option LTP now. Catch-up (decision time long past): open of the
        1-min bar at ts — the backtest's fill rule."""
        e = self.e
        key = e.contract_key(contract)
        ts = pd.Timestamp(ts)
        if (pd.Timestamp(e.clock()) - ts).total_seconds() > 60:
            tok = e.instr.get(key, {}).get("token")
            with e.lock:
                book = e.books.get(tok)
                px = book.open_at(ts) if book else None
            if px is None:
                return None
            e.catchup_fills.add((key, ts))
            return ts, float(px)
        px = e.price(key)
        return (pd.Timestamp(e.clock()).floor("s"), px) if px is not None else None

    def last_price(self, day, contract, ts):
        e = self.e
        key = e.contract_key(contract)
        px = e.price(key)
        if px is not None:
            return pd.Timestamp(e.clock()).floor("s"), px
        tok = e.instr.get(key, {}).get("token")
        with e.lock:
            book = e.books.get(tok)
            r = book.last_close_before(pd.Timestamp(ts)) if book else None
        return (r[0], float(r[1])) if r else None


# ════════════════════════════════════════════════════════════════════════════
class Engine:
    def __init__(self, clock=None):
        self.p = PARAMS
        self.clock = clock or now_ist
        self.lock = threading.RLock()     # books / ltp (tick thread)
        self.mlock = threading.RLock()    # the DayMachine (step thread vs writer)
        self.today = self.clock().date()
        self.date_str = self.today.isoformat()
        self.cfg = Config(start=self.date_str, end=self.date_str, lots=self.p["lots"],
                          capital=self.p["capital"], margin_per_lot=self.p["margin_per_lot"],
                          strike_step=self.p["strike_step"],
                          switch_sessions_left=self.p["switch_sessions_left"],
                          tag="LIVE_PAPER")
        self.kite = None
        self.kws = None
        self.fy = None
        self.spot_token = None
        self.instr: dict = {}           # (expiry_date, strike, 'CE'|'PE') -> row
        self.by_token: dict = {}
        self.expiries: list = []
        self.trade_expiry = None
        self.expiry_reason = ""
        self.lot_size = None
        self.qty = None

        self.spot_book = MinuteBook()
        self.books: dict = {}           # token -> MinuteBook
        self.tracked: set = set()       # tokens with history loaded
        self.subscribed: set = set()
        self.ltp: dict = {}             # token -> (px, monotonic)
        self.spot = None
        self.spot_at = None
        self.first_tick_seen = False
        self.last_tick_mono = None

        self.provider = LiveProvider(self)
        self.machine = DayMachine(self.date_str, self.provider, self.cfg)
        self.processed: set = set()     # candle labels HH:MM handed to the machine
        self.catchup_fills: set = set()
        self.recorded_exits: set = set()
        self.sys_events: list = []
        self.blocked_reason = None
        self.status = "starting"
        self._dirty = True
        self._last_mtm = 0.0

    # ── logging ──────────────────────────────────────────────────────────
    def event(self, kind, msg):
        e = {"t": self.clock().strftime("%H:%M:%S"), "kind": kind, "msg": msg}
        print(f"  [stdir] {e['t']} {kind}: {msg}", flush=True)
        self.sys_events.append(e)
        self.sys_events = self.sys_events[-200:]
        append_jsonl(RESULTS / f"{self.date_str}_events.jsonl", e)
        self._dirty = True

    # ── instruments / expiry ─────────────────────────────────────────────
    @staticmethod
    def contract_key(contract):
        expiry, strike, opt = contract
        return (pd.Timestamp(expiry).date(), int(strike), opt)

    def trading_days_between(self, a: dt.date, b: dt.date) -> int:
        """Sessions d with a < d <= b (weekdays minus configured NSE holidays)."""
        hol = set(self.p.get("nse_holidays", []))
        n, d = 0, a + dt.timedelta(days=1)
        while d <= b:
            if d.weekday() < 5 and d.isoformat() not in hol:
                n += 1
            d += dt.timedelta(days=1)
        return n

    def choose_expiry(self):
        front = self.expiries[0]
        left = self.trading_days_between(self.today, front)
        if left <= self.p["switch_sessions_left"]:
            self.trade_expiry = self.expiries[1]
            self.expiry_reason = (f"front {front} has {left} session(s) left "
                                  f"(<= {self.p['switch_sessions_left']}) -> next week")
        else:
            self.trade_expiry = front
            self.expiry_reason = f"front week, {left} session(s) left"

    def select_contract(self, spot):
        step = self.p["strike_step"]
        strike = int(round(spot / step) * step)
        if (self.trade_expiry, strike, "CE") not in self.instr:
            self.event("WARN", f"strike {strike} not listed for {self.trade_expiry}")
        return {"strike": strike, "expiry": pd.Timestamp(self.trade_expiry),
                "reason": self.expiry_reason}

    def connect(self):
        kx = _load("live_trading_options/strangle_strategy/live/kite_executor.py", "_stdir_kite")
        self.kx = kx
        self.kite = kx.get_kite()
        self.event("SYSTEM", f"Kite auth ok ({self.kite.profile().get('user_id')})")
        nfo = kx._instruments(self.kite, "NFO")
        rows = [r for r in nfo if r.get("name") == "NIFTY" and r.get("instrument_type") in ("CE", "PE")]
        self.expiries = sorted({r["expiry"] for r in rows if r["expiry"] >= self.today})
        if len(self.expiries) < 2:
            raise RuntimeError("fewer than two NIFTY expiries listed")
        self.choose_expiry()
        for r in rows:
            if r["expiry"] != self.trade_expiry:
                continue
            key = (r["expiry"], int(r["strike"]), r["instrument_type"])
            self.instr[key] = {"token": int(r["instrument_token"]),
                               "tradingsymbol": r["tradingsymbol"], "lot_size": int(r["lot_size"])}
            self.by_token[int(r["instrument_token"])] = key
        self.lot_size = next(iter(self.instr.values()))["lot_size"]
        self.qty = self.lot_size * self.p["lots"]
        for r in kx._instruments(self.kite, "NSE"):
            if r.get("tradingsymbol") == "NIFTY 50" and r.get("segment") == "INDICES":
                self.spot_token = int(r["instrument_token"])
                break
        if not self.spot_token:
            raise RuntimeError("NIFTY 50 index token not found")
        self.event("SYSTEM", f"trade expiry {self.trade_expiry} ({self.expiry_reason}); "
                             f"lot {self.lot_size} x {self.p['lots']} = qty {self.qty}; "
                             f"{len(self.instr)} contracts")

    # ── history (warm-up) ────────────────────────────────────────────────
    def _fyers(self):
        if self.fy is None:
            fc = _load("live_trading_options/strangle_strategy/core/fyers_client.py", "_stdir_fyers")
            self.fy = fc.get_client()
        return self.fy

    def history(self, fyers_symbol: str, kite_token: int | None, sessions: int) -> pd.DataFrame:
        """Last `sessions` prior sessions + today's completed minutes, 1-min OHLC."""
        start = self.today - dt.timedelta(days=sessions * 2 + 6)
        cutoff = pd.Timestamp(self.clock()).floor("min")
        try:
            r = self._fyers().history({"symbol": fyers_symbol, "resolution": "1", "date_format": "1",
                                       "range_from": start.isoformat(), "range_to": self.date_str,
                                       "cont_flag": "1"})
            c = (r or {}).get("candles") or []
            if not c:
                raise RuntimeError(f"empty: {str(r)[:120]}")
            df = pd.DataFrame(c, columns=["ts", "open", "high", "low", "close", "volume"])
            df["datetime"] = [dt.datetime.fromtimestamp(x, IST).replace(tzinfo=None) for x in df.ts]
        except Exception as e:
            if kite_token is None:
                raise
            self.event("WARN", f"Fyers history {fyers_symbol} failed ({e}); trying Kite")
            rows = self.kite.historical_data(kite_token, start, self.today + dt.timedelta(days=1), "minute")
            df = pd.DataFrame(rows)
            df["datetime"] = [pd.Timestamp(x).tz_localize(None) if pd.Timestamp(x).tzinfo is None
                              else pd.Timestamp(x).tz_convert(IST).tz_localize(None) for x in df["date"]]
        df = df[df["datetime"] < cutoff]
        days = sorted(df["datetime"].dt.date.unique())
        prior = [d for d in days if d < self.today][-sessions:] if sessions else []
        keep = prior + [d for d in days if d == self.today]
        return df[df["datetime"].dt.date.isin(keep)][["datetime", "open", "high", "low", "close"]]

    def seed_spot(self):
        try:
            df = self.history(SPOT_SYMBOL_FYERS, self.spot_token, self.p["history_sessions"])
        except Exception as e:
            self.blocked_reason = f"no NIFTY history for Supertrend warm-up: {e}"
            self.event("BLOCKED", self.blocked_reason)
            return
        with self.lock:
            self.spot_book.add_history(df)
        days = sorted(df["datetime"].dt.date.unique())
        prior = [d for d in days if d < self.today]
        self.event("SYSTEM", f"spot warm-up: {len(prior)} prior session(s) {prior[0] if prior else '-'}"
                             f"..{prior[-1] if prior else '-'}, {len(df)} minutes")
        if len(prior) < 1:
            self.blocked_reason = "no prior session in history — Supertrend not warmed up"
            self.event("BLOCKED", self.blocked_reason)

    def ensure_tracked(self, key):
        """Subscribe + load warm-up history for a contract (once)."""
        row = self.instr.get(key)
        if row is None:
            return
        tok = row["token"]
        with self.lock:
            if tok not in self.books:
                self.books[tok] = MinuteBook()
        self.subscribe([tok])
        if tok in self.tracked:
            return
        self.tracked.add(tok)
        try:
            # same warm-up depth as the backtest (cfg.option_warmup_days)
            df = self.history("NSE:" + row["tradingsymbol"], tok, self.cfg.option_warmup_days)
            with self.lock:
                self.books[tok].add_history(df)
        except Exception as e:
            self.event("WARN", f"history for {row['tradingsymbol']} unavailable ({e}) — "
                               f"its Supertrend warms up from ticks only")

    # ── feed ─────────────────────────────────────────────────────────────
    def band_keys(self):
        ref = self.spot
        if ref is None:
            f = self.spot_book.frame(pd.Timestamp(self.clock()))
            ref = float(f["close"].iloc[-1]) if len(f) else None
        if ref is None:
            return []
        step = self.p["strike_step"]
        atm = int(round(ref / step) * step)
        out = []
        for k in range(-self.p["band_strikes"], self.p["band_strikes"] + 1):
            for opt in ("PE", "CE"):
                key = (self.trade_expiry, atm + k * step, opt)
                if key in self.instr:
                    out.append(key)
        return out

    def subscribe(self, tokens):
        tokens = [t for t in tokens if t not in self.subscribed]
        if not tokens or self.kws is None:
            return
        try:
            self.kws.subscribe(tokens)
            opt = [t for t in tokens if t != self.spot_token]
            if opt:      # FULL: carries last_trade_time, needed to drop stale snapshots
                self.kws.set_mode(self.kws.MODE_FULL, opt)
            if self.spot_token in tokens:
                self.kws.set_mode(self.kws.MODE_LTP, [self.spot_token])
            self.subscribed.update(tokens)
            now = self.clock()
            with self.lock:
                for t in tokens:
                    book = self.spot_book if t == self.spot_token else self.books.setdefault(t, MinuteBook())
                    if book.sub_at is None:
                        book.sub_at = now
        except Exception as e:
            self.event("WARN", f"subscribe failed: {e}")

    def on_ticks(self, ws, ticks):
        try:
            now = self.clock()
            with self.lock:
                for t in ticks:
                    try:
                        tok, lp = t.get("instrument_token"), t.get("last_price")
                        if tok is None or lp is None:
                            continue
                        self.tick(int(tok), float(lp), now, t.get("last_trade_time"))
                    except Exception as e:
                        print(f"  [stdir] tick skipped: {e}", flush=True)
        except Exception as e:
            print(f"  [stdir] on_ticks error: {e}", flush=True)

    def tick(self, tok: int, lp: float, now: dt.datetime, last_trade_time=None):
        """One price update (also used by sim_test). Caller holds the lock.

        An option's first tick at 09:15 is Kite's snapshot of the PREVIOUS
        session's last price (12-Oct: 22550 CE showed 204.90 while the first
        real trade was 240.05). Counted as a trade it fakes the 09:15 candle's
        open/low -- the initial reference low -- and the option Supertrend. So
        option ticks whose last trade is before today's open are ignored."""
        in_session = MKT_OPEN <= now.time() < MKT_CLOSE
        if tok == self.spot_token:
            self.spot, self.spot_at = lp, time.monotonic()
            if in_session:
                self.spot_book.add_tick(now, lp)
                self.last_tick_mono = time.monotonic()
                if not self.first_tick_seen:
                    self.first_tick_seen = True
                    self.event("FEED", f"first NIFTY tick {lp:.2f}")
            return
        if last_trade_time is not None:
            try:
                ltt = pd.Timestamp(last_trade_time).tz_localize(None)                     if pd.Timestamp(last_trade_time).tzinfo else pd.Timestamp(last_trade_time)
                if ltt < pd.Timestamp(dt.datetime.combine(now.date(), MKT_OPEN)):
                    self.stale_ticks = getattr(self, "stale_ticks", 0) + 1
                    return
            except Exception:
                pass
        self.ltp[tok] = (lp, time.monotonic(), now)
        if in_session:
            book = self.books.get(tok)
            if book is None:
                book = self.books[tok] = MinuteBook()
            book.add_tick(now, lp)

    def on_connect(self, ws, response):
        self.event("FEED", "Kite ticker connected")
        self.subscribed.clear()
        self.subscribe([self.spot_token] + [self.instr[k]["token"] for k in self.band_keys()]
                       + list(self.books.keys()))

    def on_close(self, ws, code, reason):
        print(f"  [stdir] ticker closed: {code} {reason}", flush=True)

    def on_error(self, ws, code, reason):
        print(f"  [stdir] ticker error: {code} {reason}", flush=True)

    def price(self, key):
        """Paper fill price: fresh LTP, else a REST quote."""
        row = self.instr.get(key)
        if row is None:
            return None
        tok = row["token"]
        v = self.ltp.get(tok)
        if v is not None and time.monotonic() - v[1] < 30:
            return v[0]
        if self.kite is None:
            return v[0] if v else None
        try:
            q = self.kite.ltp([f"NFO:{row['tradingsymbol']}"])
            lp = q.get(f"NFO:{row['tradingsymbol']}", {}).get("last_price")
            if lp:
                self.ltp[tok] = (float(lp), time.monotonic(), self.clock())
                return float(lp)
        except Exception as e:
            self.event("WARN", f"quote fallback failed for {row['tradingsymbol']}: {e}")
        return v[0] if v else None

    # ── decisions ────────────────────────────────────────────────────────
    def labels(self):
        out, t = [], dt.datetime.combine(self.today, dt.time(9, 15))
        while t.time() <= dt.time(15, 10):
            out.append(t)
            t += dt.timedelta(minutes=5)
        return out

    def breakeven_tick(self, now):
        """Stop to cost, tick by tick (parameters.json `breakeven_after_points`).

        Same rule as the backtest's optional breakeven (backtested on 1-minute bars):
        once the option has traded `x` points below our sell price the stop sits at
        the sell price; a later tick at/above it buys back at that tick's price.
        Arming and the stop are separate ticks, so one tick can never do both."""
        x = self.p.get("breakeven_after_points")
        pos = self.machine.position
        if not x or pos is None or self.machine.done:
            return
        row = self.instr.get(self.contract_key(pos["contract"]))
        v = self.ltp.get(row["token"]) if row else None
        if v is None or v[2] <= pd.Timestamp(pos["entry_ts"]).to_pydatetime():
            return                                   # no tick since we sold
        px, entry = float(v[0]), float(pos["entry_price"])
        tick_ts = pd.Timestamp(v[2])
        if pos.get("be_last_tick") == tick_ts:
            return                                   # this tick was already evaluated
        pos["be_last_tick"] = tick_ts
        with self.mlock:
            n_ev = len(self.machine.events)
            if pos.get("be_armed_ts") is None:
                if px <= entry - x:
                    pos["be_armed_ts"] = tick_ts
                    self.machine.log(tick_ts, "breakeven_armed", None, pos, ltp=px,
                                     trigger=round(entry - x, 2), stop=entry)
            elif px >= entry:
                self.machine.log(tick_ts, "breakeven_stop", None, pos, ltp=px, stop=entry,
                                 armed=pos["be_armed_ts"].strftime("%H:%M:%S"))
                self.machine.close_trade(pos, tick_ts, px, "breakeven_stop", tick_ts)
                self.machine.last_closed = tick_ts
                self.machine.position = None
            if len(self.machine.events) > n_ev:
                self._after_bar(n_ev)

    def step(self):
        """Hand every newly completed candle to the machine, in order."""
        now = self.clock()
        if self.blocked_reason:
            return
        if now.time() < dt.time(15, 15):
            self.breakeven_tick(now)
        for t in self.labels():
            key = t.strftime("%H:%M")
            if key in self.processed:
                continue
            if now < t + dt.timedelta(minutes=5, seconds=GRACE):
                break
            self.processed.add(key)       # marked first: a failure is never retried later
            try:
                with self.mlock:
                    n_ev = len(self.machine.events)
                    self.machine.on_close(pd.Timestamp(t))
                    self._after_bar(n_ev)
            except Exception as e:
                self.event("ERROR", f"bar {key} failed: {type(e).__name__}: {e}")
        if now.time() >= dt.time(15, 16) and not self.machine.done:
            with self.mlock:
                n_ev = len(self.machine.events)
                self.machine.square_off()
                self._after_bar(n_ev)
            self.event("SYSTEM", "safety square-off (15:15 bar was not processed)")

    def _after_bar(self, n_ev):
        for e in self.machine.events[n_ev:]:
            print(f"  [stdir] {pd.Timestamp(e['ts']).strftime('%H:%M:%S')} {e['event']} "
                  f"{e['setup_id'] or e['trade_id'] or ''} {e['detail']}", flush=True)
            append_jsonl(RESULTS / f"{self.date_str}_events.jsonl",
                         {"t": pd.Timestamp(e["ts"]).strftime("%H:%M:%S"), "kind": e["event"],
                          "msg": f"{e['setup_id'] or e['trade_id'] or ''} {e['detail']}".strip()})
        # make sure the contract of any new setup / position streams from now on
        for c in [s.contract for s in self.machine.setups[-2:]]:
            self.ensure_tracked(self.contract_key(c))
        for tr in self.machine.trades:
            if tr["exit_price"] is not None and tr["trade_id"] not in self.recorded_exits:
                self.recorded_exits.add(tr["trade_id"])
                append_jsonl(RESULTS / "trades.jsonl", self.trade_row(tr))
                self._write_daily()
        self._persist()
        self._dirty = True

    # ── P&L ──────────────────────────────────────────────────────────────
    def trade_row(self, tr, ltp=None):
        exit_px = tr["exit_price"] if tr["exit_price"] is not None else ltp
        out = {k: (str(v) if isinstance(v, (pd.Timestamp, dt.date, dt.datetime)) else v)
               for k, v in tr.items() if k != "contract"}
        out["qty"] = self.qty
        k = self.contract_key(tr["contract"])
        out["catchup_fill"] = any(ck[0] == k and ck[1] in (pd.Timestamp(tr["entry_ts"]),
                                                          pd.Timestamp(tr["exit_ts"] or tr["entry_ts"]))
                                  for ck in self.catchup_fills)
        if exit_px is None:
            out.update(gross=None, charges=None, net=None)
            return out
        gross = (tr["entry_price"] - exit_px) * self.qty
        ch = statutory_costs(tr["entry_price"], exit_px, self.qty, self.date_str)["total"]
        out.update(ltp=exit_px, gross=round(gross, 2), charges=round(ch, 2), net=round(gross - ch, 2))
        return out

    def day_pnl(self):
        realized_g = realized_n = open_g = open_n = 0.0
        pos = None
        for tr in self.machine.trades:
            if tr["exit_price"] is not None:
                r = self.trade_row(tr)
                realized_g += r["gross"]; realized_n += r["net"]
            else:
                lp = self.price_cached(self.contract_key(tr["contract"]))
                r = self.trade_row(tr, lp)
                pos = r
                if r["gross"] is not None:
                    open_g, open_n = r["gross"], r["net"]
        return {"realized_gross": round(realized_g, 2), "realized_net": round(realized_n, 2),
                "open_gross": round(open_g, 2), "open_net": round(open_n, 2),
                "day_gross": round(realized_g + open_g, 2), "day_net": round(realized_n + open_n, 2),
                "trades": len(self.machine.trades)}, pos

    def price_cached(self, key):
        row = self.instr.get(key)
        v = self.ltp.get(row["token"]) if row else None
        return v[0] if v else None

    # ── persistence ──────────────────────────────────────────────────────
    def _persist(self):
        try:
            st = {k: v for k, v in self.machine.__dict__.items() if k != "provider"}
            blob = pickle.dumps({"machine": st, "processed": sorted(self.processed),
                                 "catchup": list(self.catchup_fills),
                                 "recorded": sorted(self.recorded_exits)})
            tmp = STATE_DIR / f"{self.date_str}_MACHINE.pkl.tmp"
            tmp.write_bytes(blob)
            os.replace(tmp, STATE_DIR / f"{self.date_str}_MACHINE.pkl")
        except Exception as e:
            print(f"  [stdir] persist error: {type(e).__name__}: {e}", flush=True)

    def restore(self):
        f = STATE_DIR / f"{self.date_str}_MACHINE.pkl"
        if not f.exists():
            return
        try:
            d = pickle.loads(f.read_bytes())
            self.machine.__dict__.update(d["machine"])
            self.machine.provider = self.provider
            self.processed = set(d["processed"])
            self.catchup_fills = set(d.get("catchup", []))
            self.recorded_exits = set(d.get("recorded", []))
            self.event("RESUME", f"restored: {len(self.machine.trades)} trade(s), position "
                                 f"{'OPEN' if self.machine.position else 'flat'}, "
                                 f"{len(self.processed)} bar(s) done")
            for s in self.machine.setups[-2:]:
                self.ensure_tracked(self.contract_key(s.contract))
        except Exception as e:
            self.event("WARN", f"could not restore {f.name}: {e} — starting fresh")

    def _prior_equity(self):
        daily = self._read_daily()
        return self.p["capital"] + sum(v["net"] for k, v in daily.items() if k < self.date_str)

    @staticmethod
    def _read_daily():
        try:
            return json.loads((RESULTS / "daily.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_daily(self):
        daily = self._read_daily()
        m, _ = self.day_pnl()
        closed = [self.trade_row(t) for t in self.machine.trades if t["exit_price"] is not None]
        prior = self.p["capital"] + sum(v["net"] for k, v in daily.items() if k < self.date_str)
        daily[self.date_str] = {
            "date": self.date_str, "trades": len(closed),
            "wins": sum(1 for t in closed if t["net"] > 0),
            "gross": m["realized_gross"], "net": m["realized_net"],
            "charges": round(sum(t["charges"] for t in closed), 2),
            "equity_close": round(prior + m["realized_net"], 2),
            "expiry": str(self.trade_expiry), "lot_size": self.lot_size, "qty": self.qty,
            "reversals": len(self.machine.reversals), "blocked": self.blocked_reason,
        }
        write_json(RESULTS / "daily.json", dict(sorted(daily.items())))

    # ── publishing ───────────────────────────────────────────────────────
    def setup_view(self, s):
        if s is None:
            return None
        return {"id": s.setup_id, "origin": s.origin, "opt": s.opt_type, "strike": s.strike,
                "expiry": str(s.expiry.date()), "created": s.created_at.strftime("%H:%M"),
                "spot": _r(s.spot_at_selection), "ref_low": _r(s.ref_low),
                "ref_initial": _r(s.ref_low_initial),
                "ref_candle": s.ref_candle_ts.strftime("%H:%M") if s.ref_candle_ts is not None else None,
                "updates": len(s.ref_updates), "status": s.status,
                "signal": s.signal_ts.strftime("%H:%M") if s.signal_ts is not None else None,
                "pending": s.pending_while_open}

    def write_tick(self):
        with self.mlock, self.lock:
            m, pos = self.day_pnl()
            mach = self.machine
            d = {"ts": self.clock().strftime("%H:%M:%S"), "date": self.date_str,
                 "status": self.status, "mode": "paper",
                 "spot": _r(self.spot), "spot_age": _r(time.monotonic() - self.spot_at, 1) if self.spot_at else None,
                 "bias": {1: "bull", -1: "bear"}.get(mach.bias),
                 "position": pos, "day": m, "setup": self.setup_view(mach.setup),
                 "equity": round(self._prior_equity() + m["day_net"], 2), "capital": self.p["capital"],
                 "blocked": self.blocked_reason, "qty": self.qty, "lots": self.p["lots"],
                 "expiry": str(self.trade_expiry),
                 "last_bar": max(self.processed) if self.processed else None}
        write_json(STATE_DIR / "TICK.json", d)
        if self.first_tick_seen and time.monotonic() - self._last_mtm >= 5 \
                and MKT_OPEN <= self.clock().time() <= dt.time(15, 31):
            self._last_mtm = time.monotonic()
            append_jsonl(RESULTS / f"{self.date_str}_mtm.jsonl",
                         {"t": d["ts"], "time": epoch(self.clock()), "spot": d["spot"],
                          "gross": m["day_gross"], "net": m["day_net"],
                          "open": bool(pos), "equity": d["equity"]})

    def _candles_json(self, c5):
        return [{"time": epoch(ts), "label": ts.strftime("%H:%M"), "open": _r(r["open"]),
                 "high": _r(r["high"]), "low": _r(r["low"]), "close": _r(r["close"]),
                 "st": _r(r["st"]), "dir": int(r["st_dir"])} for ts, r in c5.iterrows()]

    def write_state(self):
        with self.mlock:
            self._write_state()

    def _write_state(self):
        mach = self.machine
        spot5 = self.provider.spot_candles(self.date_str)
        panels, seen = [], set()
        wanted = []
        if mach.position is not None:
            wanted.append(("position", mach.position["contract"]))
        if mach.setup is not None:
            wanted.append(("setup", mach.setup.contract))
        for tr in reversed(mach.trades):
            wanted.append(("trade", tr["contract"]))
        for role, c in wanted:
            k = self.contract_key(c)
            if k in seen or len(panels) >= 4:
                continue
            seen.add(k)
            oc = self.provider.option_candles(self.date_str, c)
            setups = [s for s in mach.setups if self.contract_key(s.contract) == k]
            refs = []
            for s in setups:
                if s.ref_candle_ts is None:
                    continue
                path = [(s.ref_candle_ts, s.ref_low_initial)] + [(a, n) for a, _, n in s.ref_updates]
                end = s.signal_candle_ts or s.invalidated_at or pd.Timestamp(self.clock()).floor("5min")
                refs.append({"setup": s.setup_id, "status": s.status,
                             "points": [{"time": epoch(a), "value": _r(v)} for a, v in path]
                             + [{"time": epoch(end), "value": _r(path[-1][1])}]})
            marks = []
            for tr in mach.trades:
                if self.contract_key(tr["contract"]) != k:
                    continue
                marks.append({"time": epoch(pd.Timestamp(tr["entry_ts"]).floor("5min")), "kind": "sell",
                              "price": _r(tr["entry_price"]), "id": tr["trade_id"]})
                if tr["exit_ts"] is not None:
                    marks.append({"time": epoch(pd.Timestamp(tr["exit_ts"]).floor("5min")), "kind": "buy",
                                  "price": _r(tr["exit_price"]), "id": tr["trade_id"],
                                  "reason": tr["exit_reason"]})
            row = self.instr.get(k, {})
            panels.append({"role": role, "label": f"{k[1]} {k[2]}", "symbol": row.get("tradingsymbol"),
                           "expiry": str(k[0]), "candles": self._candles_json(oc) if not oc.empty else [],
                           "refs": refs, "markers": marks, "ltp": _r(self.price_cached(k))})
        with self.lock:
            m, pos = self.day_pnl()
            ev = [{"t": pd.Timestamp(e["ts"]).strftime("%H:%M:%S"), "kind": e["event"],
                   "msg": f"{e['setup_id'] or e['trade_id'] or ''} {e['detail']}".strip()}
                  for e in mach.events]
            events = sorted(ev + self.sys_events, key=lambda e: e["t"])[-250:]
            d = {"updated": self.clock().strftime("%Y-%m-%d %H:%M:%S"), "date": self.date_str,
                 "strategy": self.p["strategy_name"], "mode": "paper", "status": self.status,
                 "blocked": self.blocked_reason, "expiry": str(self.trade_expiry),
                 "expiry_reason": self.expiry_reason, "lot_size": self.lot_size,
                 "lots": self.p["lots"], "qty": self.qty, "capital": self.p["capital"],
                 "bias": {1: "bull", -1: "bear"}.get(mach.bias),
                 "spot_candles": self._candles_json(spot5) if not spot5.empty else [],
                 "reversals": [{"time": epoch(r["candle"]), "to": r["to"], "spot": _r(r["spot_close"])}
                               for r in mach.reversals],
                 "panels": panels,
                 "trades": [self.trade_row(t, self.price_cached(self.contract_key(t["contract"])))
                            for t in mach.trades],
                 "setups": [self.setup_view(s) for s in mach.setups],
                 "position": pos, "day": m, "events": events,
                 "feed": {"first_tick": self.first_tick_seen, "subscribed": len(self.subscribed),
                          "tracked": len(self.tracked)}}
        write_json(STATE_DIR / f"{self.date_str}_STATE.json", d)
        self._dirty = False

    # ── loops ────────────────────────────────────────────────────────────
    def step_loop(self):
        while True:
            time.sleep(0.25)
            try:
                self.step()
            except Exception as e:
                self.event("ERROR", f"step: {type(e).__name__}: {e}")
                time.sleep(1)

    def writer_loop(self):
        n, done_eod = 0, False
        while True:
            time.sleep(1)
            n += 1
            now = self.clock()
            try:
                t = now.time()
                self.status = ("pre-open" if t < MKT_OPEN else
                               "live" if t <= MKT_CLOSE and self.first_tick_seen else
                               "waiting for market data" if t <= MKT_CLOSE else "closed")
                if self.blocked_reason and t <= MKT_CLOSE:
                    self.status = "blocked (streaming only)"
                self.write_tick()
                if n % 3 == 0 or self._dirty:
                    self.write_state()
                if n % 10 == 0:
                    for k in self.band_keys():
                        tok = self.instr[k]["token"]
                        if tok not in self.subscribed:
                            with self.lock:
                                self.books.setdefault(tok, MinuteBook())
                            self.subscribe([tok])
            except Exception as e:
                print(f"  [stdir] writer error: {type(e).__name__}: {e}", flush=True)
            if not done_eod and now.time() >= dt.time(15, 31):
                done_eod = True
                try:
                    if self.first_tick_seen:
                        self._write_daily()
                    self.event("SYSTEM", "session closed — final state written")
                    self.write_state()
                except Exception as e:
                    print(f"  [stdir] EOD error: {e}", flush=True)
            if now.time() >= dt.time(15, 36):
                print("  [stdir] exiting after close", flush=True)
                os._exit(0)
            if (self.first_tick_seen and self.last_tick_mono is not None
                    and time.monotonic() - self.last_tick_mono > 120
                    and dt.time(9, 20) < now.time() < dt.time(15, 29)):
                self.event("ERROR", "no NIFTY tick for 120 s — exiting for a clean restart")
                self._persist()
                os._exit(1)


# ════════════════════════════════════════════════════════════════════════════
def main():
    singleton = _load("live_trading_options/strangle_strategy/core/singleton.py", "_stdir_singleton")
    if not singleton.acquire(PORT_LOCK):
        print("  [stdir] another engine holds the lock — this duplicate exits.", flush=True)
        return
    eng = Engine()
    print(f"\n  ── Nifty ST Directional (PAPER) — {eng.date_str} ──", flush=True)
    if eng.today.weekday() >= 5:
        print("  [stdir] weekend — nothing to do.", flush=True)
        return
    try:
        eng.connect()
    except Exception as e:
        eng.status = "error"
        eng.event("ABORT", f"Kite unavailable: {type(e).__name__}: {e}")
        eng.write_tick()
        return

    from kiteconnect import KiteTicker
    eng.kws = KiteTicker(eng.kx._API_KEY, eng.kx.access_token())
    eng.kws.on_ticks = eng.on_ticks
    eng.kws.on_connect = eng.on_connect
    eng.kws.on_close = eng.on_close
    eng.kws.on_error = eng.on_error
    eng.kws.connect(threaded=True)
    for _ in range(50):                 # ticks start streaming before history lands
        if eng.subscribed:
            break
        time.sleep(0.2)

    eng.seed_spot()
    for k in eng.band_keys():           # warm-up for the strikes a setup can pick
        eng.ensure_tracked(k)
    eng.restore()
    eng.write_state()
    threading.Thread(target=eng.writer_loop, daemon=True, name="stdir-writer").start()
    eng.step_loop()


if __name__ == "__main__":
    main()
