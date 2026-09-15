"""
engine.py — Nifty Directional Pivot, live PAPER engine.
======================================================

STANDALONE process. Takes market data from its own **Kite** ticker and never opens
a Fyers socket: the VWAP strangle owns that, and a second Fyers connection on the
same token risks the broker dropping one (see delta_neutral/engine.py). Fyers is
used only for a single REST history call at startup, which does not touch sockets.

Every decision is made by core/strategy.py — the same classes replay.py proves
reproduce the backtest trade-for-trade. This file only turns ticks into the inputs
those classes expect, and publishes what happened.

    ticks ─► 1-min closes (last price in each minute) ─► 5-min bars + Supertrend(14,2.5)
          ─► PivotCore.on_bar() at each 5-min boundary       (entries, ST exits, 15:15)
          ─► PivotCore.on_option_tick() on every option tick (2x premium stop)

Published for the dashboard (read-only consumer: deployment/pivot_api.py):
    data/live_state/TICK.json            ~1s   spot, position, live MTM
    data/live_state/{date}_STATE.json    ~3s   candles, pivots, Supertrend, trades, events
    data/live_state/{date}_CORE.json     on change   restart-recovery snapshot
    data/results/daily.json              per-day summary -> equity curve + days table
    data/results/trades.jsonl            every closed trade, append-only
    data/results/{date}_mtm.jsonl        intraday MTM samples
    data/seed/nifty_minutes.parquet      rolling minute-close history (self-maintained)

Fail-closed: if the previous session's H/L/C cannot be established with confidence,
the engine still streams and charts, but takes no trades that day and says why.

Run (VPS venv python):
    .venv\\Scripts\\python.exe -u live_trading_options\\nifty_pivot\\engine.py
Single-instance lock: 127.0.0.1:47656.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
import time
import calendar
import threading
import importlib.util
import datetime as dt
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT))

from core.strategy import SignalBuilder, PivotCore, previous_weekday, PUT, CALL   # noqa: E402

PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text(encoding="utf-8"))
STATE_DIR = ROOT / "data" / "live_state"
RESULTS = ROOT / "data" / "results"
SEED_FILE = ROOT / "data" / "seed" / "nifty_minutes.parquet"
for d in (STATE_DIR, RESULTS, SEED_FILE.parent):
    d.mkdir(parents=True, exist_ok=True)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PORT_PIVOT_ENGINE = 47656
MKT_OPEN, MKT_CLOSE = dt.time(9, 15), dt.time(15, 30)
FIRST_LABEL, LAST_LABEL = dt.time(9, 20), dt.time(15, 15)
BAND = 8                     # strikes each side of ATM kept streaming
MINUTE_GRACE = 1.2           # seconds after a minute ends before it is finalised


def _load(rel: str, name: str):
    """Load a shared module by explicit file path. Several strategy packages have a
    `core/` or `live/` subpackage; a plain import could silently resolve to another
    strategy's module."""
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


singleton = _load("live_trading_options/strangle_strategy/core/singleton.py", "_ndp_singleton")
kx = _load("live_trading_options/strangle_strategy/live/kite_executor.py", "_ndp_kite")


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


def epoch(ts) -> int:
    """IST wall time as a UTC epoch — lightweight-charts has no timezone option, so
    this makes the axis read in IST."""
    return calendar.timegm(pd.Timestamp(ts).timetuple())


def write_json(path: Path, obj) -> bool:
    """Atomic write that never raises.

    On Windows os.replace() fails with "Access is denied" whenever another process
    holds the destination open for a moment — antivirus, the dashboard reading it,
    or the VPS backup task copying trade data. That was hit in testing. So: retry
    the swap briefly, then fall back to a direct write, and report failure instead
    of throwing into the trading loop."""
    data = json.dumps(obj, default=str)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
    except Exception as e:
        print(f"  [ndp] write failed {path.name}: {e}", flush=True)
        return False
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(0.05)
        except Exception as e:
            print(f"  [ndp] replace failed {path.name}: {e}", flush=True)
            break
    try:
        path.write_text(data, encoding="utf-8")
        return True
    except Exception as e:
        print(f"  [ndp] direct write failed {path.name}: {e}", flush=True)
        return False


def append_jsonl(path: Path, obj) -> bool:
    line = json.dumps(obj, default=str) + "\n"
    for _ in range(20):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
            return True
        except PermissionError:
            time.sleep(0.05)
        except Exception as e:
            print(f"  [ndp] append failed {path.name}: {e}", flush=True)
            return False
    print(f"  [ndp] append gave up {path.name}", flush=True)
    return False


# ════════════════════════════════════════════════════════════════════════════
class Engine:
    def __init__(self):
        self.p = PARAMS
        self.lock = threading.RLock()
        self.today = now_ist().date()
        self.date_str = self.today.isoformat()

        self.kite = None
        self.kws = None
        self.expiry = None
        self.lot_size = None
        self.spot_token = None
        self.contracts: dict[tuple, dict] = {}      # (strike, PUT|CALL) -> contract
        self.by_token: dict[int, tuple] = {}
        self.subscribed: set[int] = set()
        self.ltp: dict[tuple, float] = {}
        self.ltp_at: dict[tuple, float] = {}

        self.sb = SignalBuilder(self.p["supertrend_period"], self.p["supertrend_multiplier"])
        self.core = PivotCore(self.p)
        self.levels = None
        self.prev_session = None
        self.seed_source = None
        self.blocked_reason = None

        self.spot = None
        self.spot_at = None
        self.cur_min = None
        self.cur_px = None
        self.today_minutes: dict = {}
        self.first_tick_seen = False
        self.last_tick_mono = None
        self.processed_labels: set[str] = set()
        self.skipped_before = None
        self.events: list[dict] = []
        self.status = "starting"
        self._dirty = True
        self._last_mtm_sample = 0.0

    # ── logging ──────────────────────────────────────────────────────────
    def event(self, kind: str, msg: str, **kw):
        e = {"t": now_ist().strftime("%H:%M:%S"), "kind": kind, "msg": msg, **kw}
        print(f"  [ndp] {e['t']} {kind}: {msg}", flush=True)
        self.events.append(e)
        self.events = self.events[-200:]
        append_jsonl(RESULTS / f"{self.date_str}_events.jsonl", e)
        self._dirty = True

    # ════════════════════════════════════════════════════════════════════
    # startup
    # ════════════════════════════════════════════════════════════════════
    def connect_kite(self):
        self.kite = kx.get_kite()
        who = self.kite.profile().get("user_id")
        self.event("SYSTEM", f"Kite auth ok ({who})")

    def load_contracts(self):
        nfo = kx._instruments(self.kite, "NFO")
        rows = [r for r in nfo if r.get("name") == "NIFTY"
                and r.get("instrument_type") in ("CE", "PE")]
        exps = sorted({r["expiry"] for r in rows if r["expiry"] >= self.today})
        if not exps:
            raise RuntimeError("no NIFTY expiry on/after today in Kite's dump")
        self.expiry = exps[0]
        for r in rows:
            if r["expiry"] != self.expiry:
                continue
            key = (int(r["strike"]), PUT if r["instrument_type"] == "PE" else CALL)
            tok = int(r["instrument_token"])
            self.contracts[key] = {"token": tok, "tradingsymbol": r["tradingsymbol"],
                                   "lot_size": int(r["lot_size"])}
            self.by_token[tok] = key
        self.lot_size = next(iter(self.contracts.values()))["lot_size"]
        for r in kx._instruments(self.kite, "NSE"):
            if r.get("tradingsymbol") == "NIFTY 50" and r.get("segment") == "INDICES":
                self.spot_token = int(r["instrument_token"])
                break
        if not self.spot_token:
            raise RuntimeError("NIFTY 50 index token not found in Kite's NSE dump")
        self.event("SYSTEM", f"front expiry {self.expiry} (DTE {(self.expiry - self.today).days}), "
                             f"lot size {self.lot_size}, qty {self.p['lots'] * self.lot_size}, "
                             f"{len(self.contracts)} contracts")

    # ── minute-close history ─────────────────────────────────────────────
    def _history_fyers(self, start: dt.date) -> pd.Series:
        fc = _load("live_trading_options/strangle_strategy/core/fyers_client.py", "_ndp_fyers")
        fy = fc.get_client()
        r = fy.history({"symbol": "NSE:NIFTY50-INDEX", "resolution": "1", "date_format": "1",
                        "range_from": start.isoformat(), "range_to": self.date_str,
                        "cont_flag": "1"})
        candles = (r or {}).get("candles") or []
        if not candles:
            raise RuntimeError(f"Fyers history empty: {str(r)[:160]}")
        idx = [dt.datetime.fromtimestamp(c[0], IST).replace(tzinfo=None) for c in candles]
        return pd.Series([float(c[4]) for c in candles], index=pd.DatetimeIndex(idx))

    def _history_kite(self, start: dt.date) -> pd.Series:
        rows = self.kite.historical_data(self.spot_token, start, self.today + dt.timedelta(days=1),
                                         "minute")
        if not rows:
            raise RuntimeError("Kite history empty")
        idx = [pd.Timestamp(r["date"]).tz_localize(None) if pd.Timestamp(r["date"]).tzinfo is None
               else pd.Timestamp(r["date"]).tz_convert(IST).tz_localize(None) for r in rows]
        return pd.Series([float(r["close"]) for r in rows], index=pd.DatetimeIndex(idx))

    def seed(self):
        """Minute closes for Supertrend warm-up and yesterday's H/L/C.

        Authority order: Fyers REST history -> Kite history -> local seed cache.
        An API result is authoritative about which days were sessions. The cache is
        not (it cannot tell a holiday from a day the engine missed), so on cache-only
        days the previous session must be exactly the previous weekday or trading is
        blocked for the day."""
        start = self.today - dt.timedelta(days=45)
        cache = pd.Series(dtype="float64")
        if SEED_FILE.exists():
            c = pd.read_parquet(SEED_FILE)
            cache = c.set_index("datetime")["close"].sort_index()

        api, src = None, None
        for name, fn in (("fyers", self._history_fyers), ("kite", self._history_kite)):
            try:
                api, src = fn(start), name
                break
            except Exception as e:
                self.event("WARN", f"{name} history unavailable: {type(e).__name__}: {str(e)[:120]}")

        cutoff = pd.Timestamp(now_ist()).floor("min")        # drop the still-forming minute
        if api is not None:
            api = api[api.index < cutoff]
            # A session the cache already holds in full keeps the cache's minutes: the
            # cache is the validated backtest convention (includes the 15:30 minute).
            # The API only fills sessions the cache lacks, plus today.
            full = {d for d, n in cache.groupby(cache.index.normalize()).size().items() if n >= 370} \
                if len(cache) else set()
            api_keep = api[~api.index.normalize().isin(full)]
            merged = pd.concat([cache, api_keep])
            used = sorted({d.date() for d in api_keep.index.normalize()})
            self.seed_source = (f"cache + {src} history for {len(used)} session(s)"
                                + (f" ({used[0]}..{used[-1]})" if used else ""))
        else:
            merged = cache[cache.index < cutoff]
            self.seed_source = "local seed cache only"

        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self.sb.load_series(merged)
        self.sb.trim(self.p["seed_sessions"] + 1)

        # today's minutes that already exist (mid-day restart) feed the forming state
        for ts, v in merged[merged.index.normalize() == pd.Timestamp(self.today)].items():
            self.today_minutes[ts] = float(v)

        self.levels, self.prev_session = self.sb.pivots_for(self.today)
        sessions = [s.date() for s in self.sb.sessions() if s.date() < self.today]
        if self.levels is None:
            self.blocked_reason = "no previous session in history — cannot compute pivots"
        elif len(sessions) < 5:
            self.blocked_reason = f"only {len(sessions)} sessions of history — Supertrend not warmed up"
        elif api is None and self.prev_session != previous_weekday(self.today):
            self.blocked_reason = (f"history APIs down and cached previous session is "
                                   f"{self.prev_session}, not {previous_weekday(self.today)} — "
                                   f"cannot confirm yesterday's H/L/C")
        self.event("SYSTEM", f"seed: {self.seed_source}; {len(sessions)} prior sessions; "
                             f"previous session {self.prev_session}")
        if self.levels:
            L = self.levels
            self.event("LEVELS", f"PP {L['pp']:.2f}  R1 {L['r1']:.2f}  S1 {L['s1']:.2f}  "
                                 f"(prev H {L['prev_high']:.2f} L {L['prev_low']:.2f} C {L['prev_close']:.2f})")
        if self.blocked_reason:
            self.event("BLOCKED", f"no trading today: {self.blocked_reason}")

    def restore_or_start(self):
        core_file = STATE_DIR / f"{self.date_str}_CORE.json"
        restored = False
        if core_file.exists():
            try:
                d = json.loads(core_file.read_text(encoding="utf-8"))
                self.core.restore(d)
                self.processed_labels = set(d.get("processed_labels", []))
                restored = True
                self.event("RESUME", f"restored today's book: {len(self.core.trades)} closed, "
                                     f"position {'OPEN' if self.core.pos else 'flat'}, "
                                     f"entries used {self.core.entries_used}")
            except Exception as e:
                self.event("WARN", f"could not restore {core_file.name}: {e} — starting fresh")
        if not restored:
            self.core.start_day(self.date_str, self.levels or {"pp": 0, "r1": 0, "s1": 0},
                                self.lot_size)

        # Bars that closed while this process was not running are SKIPPED, never
        # replayed: replaying them now would fill past signals at today's current
        # premiums, which is a fiction. An open position stays protected — the tick
        # stop applies immediately and the next bar re-checks Supertrend.
        now = now_ist()
        missed = [l for l in self.labels()
                  if dt.datetime.combine(self.today, l) + dt.timedelta(seconds=MINUTE_GRACE) <= now
                  and l.strftime("%H:%M") not in self.processed_labels]
        if missed:
            self.processed_labels.update(l.strftime("%H:%M") for l in missed)
            self.skipped_before = missed[-1].strftime("%H:%M")
            self.event("WARN", f"{'resumed' if restored else 'started'} mid-session: "
                               f"{len(missed)} bar(s) {missed[0]:%H:%M}–{missed[-1]:%H:%M} closed while "
                               f"offline and are NOT traded retroactively")

    @staticmethod
    def labels() -> list[dt.time]:
        out, t = [], dt.datetime.combine(dt.date.today(), FIRST_LABEL)
        while t.time() <= LAST_LABEL:
            out.append(t.time())
            t += dt.timedelta(minutes=5)
        return out

    # ════════════════════════════════════════════════════════════════════
    # feed
    # ════════════════════════════════════════════════════════════════════
    def band_tokens(self) -> list[int]:
        out = []
        if self.spot_token and self.spot_token not in self.subscribed:
            out.append(self.spot_token)
        ref = self.spot if self.spot is not None else (self.levels or {}).get("prev_close")
        if ref is None:
            return out
        step = self.p["strike_step"]
        atm = int(round(ref / step) * step)
        for k in range(-BAND, BAND + 1):
            for opt in (PUT, CALL):
                c = self.contracts.get((atm + k * step, opt))
                if c and c["token"] not in self.subscribed:
                    out.append(c["token"])
        if self.core.pos:
            c = self.contracts.get((self.core.pos["strike"], self.core.pos["option_type"]))
            if c and c["token"] not in self.subscribed:
                out.append(c["token"])
        return out

    def subscribe(self, tokens: list[int]):
        tokens = [t for t in tokens if t not in self.subscribed]
        if not tokens or self.kws is None:
            return
        try:
            self.kws.subscribe(tokens)
            opt = [t for t in tokens if t != self.spot_token]
            if opt:
                self.kws.set_mode(self.kws.MODE_LTP, opt)
            if self.spot_token in tokens:
                self.kws.set_mode(self.kws.MODE_FULL, [self.spot_token])
            self.subscribed.update(tokens)
        except Exception as e:
            self.event("WARN", f"subscribe failed: {e}")

    def on_ticks(self, ws, ticks):
        """A bad tick must never escape and kill the socket's read loop."""
        try:
            with self.lock:
                for t in ticks:
                    try:
                        tok, lp = t.get("instrument_token"), t.get("last_price")
                        if tok is None or lp is None:
                            continue
                        tok, lp = int(tok), float(lp)
                        if tok == self.spot_token:
                            self._on_spot(lp, t.get("exchange_timestamp") or t.get("timestamp"))
                        else:
                            key = self.by_token.get(tok)
                            if key:
                                self.ltp[key] = lp
                                self.ltp_at[key] = time.monotonic()
                    except Exception as e:
                        print(f"  [ndp] tick skipped: {e}", flush=True)
        except Exception as e:
            print(f"  [ndp] on_ticks error: {e}", flush=True)

    def _on_spot(self, price: float, exch_ts):
        ts = now_ist()
        if isinstance(exch_ts, dt.datetime):
            ts = exch_ts.replace(tzinfo=None)
        if not (MKT_OPEN <= ts.time() <= dt.time(15, 30, 59)):
            self.spot, self.spot_at = price, time.monotonic()
            return
        self.spot, self.spot_at = price, time.monotonic()
        self.last_tick_mono = time.monotonic()
        if not self.first_tick_seen:
            self.first_tick_seen = True
            self.event("FEED", f"first NIFTY tick {price:.2f}")
        m = pd.Timestamp(ts).floor("min")
        if self.cur_min is None:
            self.cur_min, self.cur_px = m, price
        elif m > self.cur_min:
            self._finalize_minute()
            self.cur_min, self.cur_px = m, price
        elif m == self.cur_min:
            self.cur_px = price
        # a tick stamped for an already-finalised minute is ignored

    def _finalize_minute(self):
        if self.cur_min is None or self.cur_px is None:
            return
        self.sb.set_minute(self.cur_min, self.cur_px)
        self.today_minutes[self.cur_min] = self.cur_px
        self._dirty = True

    def on_connect(self, ws, response):
        self.event("FEED", "Kite ticker connected")
        self.subscribed.clear()             # a reconnect loses subscriptions server-side
        self.subscribe(self.band_tokens())
        if self.core.pos:
            self.subscribe(self.band_tokens())

    def on_close(self, ws, code, reason):
        print(f"  [ndp] ticker closed: {code} {reason}", flush=True)

    def on_error(self, ws, code, reason):
        print(f"  [ndp] ticker error: {code} {reason}", flush=True)

    # ════════════════════════════════════════════════════════════════════
    # decisions
    # ════════════════════════════════════════════════════════════════════
    def price(self, strike: int, opt: str):
        """Paper fill: the option's LTP now. Falls back to a REST quote when the
        contract has not ticked yet (e.g. ATM moved to a strike just subscribed)."""
        key = (int(strike), opt)
        c = self.contracts.get(key)
        if c is None:
            return None
        if c["token"] not in self.subscribed:
            self.subscribe([c["token"]])
        v, at = self.ltp.get(key), self.ltp_at.get(key)
        if v is not None and at is not None and time.monotonic() - at < 30:
            return v
        try:
            q = self.kite.ltp([f"NFO:{c['tradingsymbol']}"])
            lp = q.get(f"NFO:{c['tradingsymbol']}", {}).get("last_price")
            if lp:
                self.ltp[key], self.ltp_at[key] = float(lp), time.monotonic()
                return float(lp)
        except Exception as e:
            self.event("WARN", f"quote fallback failed for {c['tradingsymbol']}: {e}")
        return v

    def step(self):
        now = now_ist()
        with self.lock:
            # finalise a minute once the clock has moved past it, even with no new tick
            if (self.cur_min is not None
                    and (now - self.cur_min.to_pydatetime()).total_seconds() >= 60 + MINUTE_GRACE):
                self._finalize_minute()
                self.cur_min, self.cur_px = None, None

            # tick-level 2x premium stop
            if self.core.pos is not None:
                key = (self.core.pos["strike"], self.core.pos["option_type"])
                lp = self.ltp.get(key)
                if lp is not None:
                    ev = self.core.on_option_tick(now, lp, self.spot or 0.0)
                    if ev:
                        self._record_exit(ev)

            # 5-min boundaries. A bar is marked done BEFORE it is processed: if anything
            # in processing fails, it must not be retried a moment later with later
            # prices — that would fill an old signal at a price it never saw.
            for lab in self.labels():
                key = lab.strftime("%H:%M")
                if key in self.processed_labels:
                    continue
                t_lab = dt.datetime.combine(self.today, lab)
                if now < t_lab + dt.timedelta(seconds=MINUTE_GRACE):
                    break
                self.processed_labels.add(key)
                try:
                    self._process_bar(t_lab)
                except Exception as e:
                    self.event("ERROR", f"bar {key} processing failed: {type(e).__name__}: {e}")

            # safety net: never carry a paper position past square-off
            if self.core.pos is not None and now.time() >= dt.time(15, 16):
                key = (self.core.pos["strike"], self.core.pos["option_type"])
                ev = self.core.force_close(now, self.ltp.get(key), self.spot or 0.0, "squareoff_safety")
                if ev:
                    self._record_exit(ev)

    def _process_bar(self, t_lab: dt.datetime):
        if not self.first_tick_seen:
            return
        bars = self.sb.bars()
        ts = pd.Timestamp(t_lab)
        if bars.empty or ts not in bars.index:
            self.event("WARN", f"bar {t_lab:%H:%M} missing (no minutes in window) — skipped")
            return
        row = bars.loc[ts]
        if int(row["dir"]) == 0:
            self.event("WARN", f"bar {t_lab:%H:%M}: Supertrend not warmed up — skipped")
            return
        if self.blocked_reason:
            return
        events = self.core.on_bar(ts, float(row["close"]), int(row["dir"]), float(row["st"]),
                                  self.price, high_fn=None)
        for ev in events:
            if ev["type"] == "entry":
                self.subscribe(self.band_tokens())
                side = "PUT" if ev["option_type"] == PUT else "CALL"
                self.event("ENTRY", f"SELL {self.core.qty} NIFTY {ev['strike']} {side} @ "
                                    f"{ev['entry_price']:.2f}  (bar {t_lab:%H:%M}, spot "
                                    f"{ev['spot_at_entry']:.2f}, stop {ev['stop_level']:.2f})",
                           trade=self._round(ev))
            else:
                self._record_exit(ev)
        self._persist_core()

    def _record_exit(self, ev: dict):
        side = "PUT" if ev["option_type"] == PUT else "CALL"
        self.event("EXIT", f"BUY back {ev['strike']} {side} @ {ev['exit_price']:.2f} "
                           f"[{ev['exit_reason']}]  gross Rs {ev['pnl_gross']:,.0f}  "
                           f"net Rs {ev['pnl_net']:,.0f}", trade=self._round(ev))
        append_jsonl(RESULTS / "trades.jsonl", self._round(ev))
        self._persist_core()
        try:
            self._write_daily()
        except Exception as e:
            print(f"  [ndp] daily write error: {type(e).__name__}: {e}", flush=True)

    @staticmethod
    def _round(d: dict) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}

    # ════════════════════════════════════════════════════════════════════
    # publishing
    # ════════════════════════════════════════════════════════════════════
    def _persist_core(self):
        try:
            d = self.core.to_dict()
            d["processed_labels"] = sorted(self.processed_labels)
            if not write_json(STATE_DIR / f"{self.date_str}_CORE.json", d):
                print("  [ndp] CORE snapshot not written this time — will retry", flush=True)
        except Exception as e:
            print(f"  [ndp] persist error: {type(e).__name__}: {e}", flush=True)

    def _prior_equity(self) -> float:
        daily = self._read_daily()
        return self.p["capital"] + sum(v["net"] for k, v in daily.items() if k < self.date_str)

    @staticmethod
    def _read_daily() -> dict:
        f = RESULTS / "daily.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_daily(self):
        daily = self._read_daily()
        tr = self.core.trades
        m = self.core.mtm(None)
        prior = self.p["capital"] + sum(v["net"] for k, v in daily.items() if k < self.date_str)
        wins = sum(1 for t in tr if t["pnl_net"] > 0)
        daily[self.date_str] = {
            "date": self.date_str, "trades": len(tr), "wins": wins,
            "gross": round(m["realized_gross"], 2), "net": round(m["realized_net"], 2),
            "charges": round(sum(t["pnl_charges"] for t in tr), 2),
            "slippage": round(sum(t["pnl_slippage"] for t in tr), 2),
            "equity_close": round(prior + m["realized_net"], 2),
            "expiry": str(self.expiry), "lot_size": self.lot_size, "qty": self.core.qty,
            "levels": {k: round(v, 2) for k, v in (self.levels or {}).items()},
            "blocked": self.blocked_reason,
            "open_position": self.core.pos is not None,
        }
        write_json(RESULTS / "daily.json", dict(sorted(daily.items())))

    def _position_view(self):
        pos = self.core.pos
        if pos is None:
            return None
        key = (pos["strike"], pos["option_type"])
        lp = self.ltp.get(key)
        at = self.ltp_at.get(key)
        m = self.core.mtm(lp)
        return {
            "side": pos["side"], "strike": pos["strike"], "option_type": pos["option_type"],
            "symbol": self.contracts.get(key, {}).get("tradingsymbol"),
            "entry_time": pos["entry_time"][11:16], "entry": round(pos["entry_price"], 2),
            "ltp": round(lp, 2) if lp is not None else None,
            "ltp_age": round(time.monotonic() - at, 1) if at else None,
            "stop": round(pos["stop_level"], 2), "qty": pos["qty"],
            "mtm_gross": m["open_gross"], "mtm_net": m["open_net"],
            "to_stop_pct": round((pos["stop_level"] - lp) / pos["stop_level"] * 100, 1) if lp else None,
        }

    def write_tick(self):
        with self.lock:
            pos = self._position_view()
            key = (self.core.pos["strike"], self.core.pos["option_type"]) if self.core.pos else None
            m = self.core.mtm(self.ltp.get(key) if key else None)
            prior = self._prior_equity()
            d = {
                "ts": now_ist().strftime("%H:%M:%S"), "date": self.date_str,
                "status": self.status, "mode": "paper",
                "spot": round(self.spot, 2) if self.spot is not None else None,
                "spot_age": round(time.monotonic() - self.spot_at, 1) if self.spot_at else None,
                "position": pos, "day": m,
                "entries_used": self.core.entries_used,
                "max_entries": self.core.max_entries,
                "equity": round(prior + m["day_net"], 2), "capital": self.p["capital"],
                "blocked": self.blocked_reason,
                "last_bar": self.core.last_bar,
            }
        write_json(STATE_DIR / "TICK.json", d)
        if time.monotonic() - self._last_mtm_sample >= 30 and self.first_tick_seen:
            self._last_mtm_sample = time.monotonic()
            append_jsonl(RESULTS / f"{self.date_str}_mtm.jsonl",
                         {"t": d["ts"], "spot": d["spot"], "gross": m["day_gross"],
                          "net": m["day_net"], "equity": d["equity"]})

    def write_state(self):
        with self.lock:
            bars = self.sb.bars()
            day = bars[bars.index.normalize() == pd.Timestamp(self.today)] if not bars.empty else bars
            candles = [{"time": epoch(ts), "label": ts.strftime("%H:%M"),
                        "open": round(r["open"], 2), "high": round(r["high"], 2),
                        "low": round(r["low"], 2), "close": round(r["close"], 2),
                        "st": round(r["st"], 2) if pd.notna(r["st"]) else None,
                        "dir": int(r["dir"])}
                       for ts, r in day.iterrows()]
            # the still-forming bar, from finalised minutes plus the live price
            forming = None
            if self.spot is not None and self.first_tick_seen:
                now = pd.Timestamp(now_ist()).floor("min")
                mins_since = int((now - pd.Timestamp(dt.datetime.combine(self.today, MKT_OPEN)))
                                 .total_seconds() // 60)
                if 0 <= mins_since < 375:
                    label = pd.Timestamp(dt.datetime.combine(self.today, MKT_OPEN)) + \
                        pd.Timedelta(minutes=(min(mins_since // 5, 74) + 1) * 5)
                    start = label - pd.Timedelta(minutes=5)
                    vals = [v for k, v in self.today_minutes.items() if start <= k < label]
                    vals.append(self.spot)
                    forming = {"time": epoch(label), "label": label.strftime("%H:%M"),
                               "open": round(vals[0], 2), "high": round(max(vals), 2),
                               "low": round(min(vals), 2), "close": round(vals[-1], 2)}
            markers = []
            for t in self.core.trades + ([self.core.pos] if self.core.pos else []):
                markers.append({"time": epoch(t["entry_time"]), "kind": "entry",
                                "side": t["side"], "strike": t["strike"],
                                "price": round(t["entry_price"], 2), "spot": t["spot_at_entry"]})
                if "exit_time" in t:
                    markers.append({"time": epoch(t["exit_time"]), "kind": "exit",
                                    "side": t["side"], "strike": t["strike"],
                                    "price": round(t["exit_price"], 2), "spot": t["spot_at_exit"],
                                    "reason": t["exit_reason"], "net": t["pnl_net"]})
            key = (self.core.pos["strike"], self.core.pos["option_type"]) if self.core.pos else None
            d = {
                "updated": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                "date": self.date_str, "strategy": self.p["strategy_name"], "mode": "paper",
                "status": self.status, "blocked": self.blocked_reason,
                "expiry": str(self.expiry), "dte": (self.expiry - self.today).days if self.expiry else None,
                "lot_size": self.lot_size, "lots": self.p["lots"], "qty": self.core.qty,
                "capital": self.p["capital"],
                "params": {k: self.p[k] for k in ("supertrend_period", "supertrend_multiplier",
                                                   "max_entries_per_day", "last_entry_time",
                                                   "squareoff_time", "premium_stop_mult")},
                "levels": {k: round(v, 2) for k, v in (self.levels or {}).items()},
                "prev_session": str(self.prev_session), "seed_source": self.seed_source,
                "skipped_before": self.skipped_before,
                "candles": candles, "forming": forming, "markers": markers,
                "trades": [self._round(t) for t in self.core.trades],
                "position": self._position_view(),
                "day": self.core.mtm(self.ltp.get(key) if key else None),
                "entries_used": self.core.entries_used,
                "events": self.events[-80:],
                "feed": {"first_tick": self.first_tick_seen,
                         "subscribed": len(self.subscribed),
                         "spot_age": round(time.monotonic() - self.spot_at, 1) if self.spot_at else None},
            }
        write_json(STATE_DIR / f"{self.date_str}_STATE.json", d)
        self._dirty = False

    def save_seed(self):
        """Append today's minute closes to the rolling seed so tomorrow's warm-up and
        pivots never depend on a history API."""
        with self.lock:
            self._finalize_minute()
            s = self.sb.series()
        if len(s):
            keep = sorted(s.index.normalize().unique())[-(self.p["seed_sessions"] + 5):]
            s = s[s.index.normalize() >= keep[0]]
        pd.DataFrame({"datetime": s.index, "close": s.values}).to_parquet(SEED_FILE, index=False)
        self.event("SYSTEM", f"seed saved: {s.index.normalize().nunique()} sessions -> {SEED_FILE.name}")

    # ════════════════════════════════════════════════════════════════════
    # loops
    # ════════════════════════════════════════════════════════════════════
    def step_loop(self):
        while True:
            time.sleep(0.2)
            try:
                self.step()
            except Exception as e:
                self.event("ERROR", f"step: {type(e).__name__}: {e}")
                time.sleep(1)

    def writer_loop(self):
        STALL = 120
        n = 0
        done_eod = False
        while True:
            time.sleep(1)
            n += 1
            now = now_ist()
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
                    with self.lock:
                        add = self.band_tokens()
                    self.subscribe(add)
                if n % 15 == 0:
                    self._persist_core()
            except Exception as e:
                print(f"  [ndp] writer error: {type(e).__name__}: {e}", flush=True)

            if not done_eod and now.time() >= dt.time(15, 31):
                done_eod = True
                try:
                    if self.first_tick_seen:        # a holiday must not become a "day traded"
                        self._write_daily()
                    self.write_state()
                    if self.first_tick_seen:
                        self.save_seed()
                    self.event("SYSTEM", "session closed — final state written")
                    self.write_state()
                except Exception as e:
                    print(f"  [ndp] EOD error: {e}", flush=True)
            if now.time() >= dt.time(15, 36):
                print("  [ndp] exiting after close", flush=True)
                os._exit(0)

            # a live socket that has gone silent: exit so the watchdog restarts us.
            # Only once the market has actually ticked today — a holiday never ticks,
            # and must not restart-loop.
            if (self.first_tick_seen and self.last_tick_mono is not None
                    and time.monotonic() - self.last_tick_mono > STALL
                    and dt.time(9, 20) < now.time() < dt.time(15, 29)):
                self.event("ERROR", f"no NIFTY tick for {STALL}s — exiting for a clean restart")
                self._persist_core()
                os._exit(1)


# ════════════════════════════════════════════════════════════════════════════
def main():
    if not singleton.acquire(PORT_PIVOT_ENGINE):
        print("  [ndp] another engine holds the lock — this duplicate exits.", flush=True)
        return

    eng = Engine()
    print(f"\n  ── Nifty Directional Pivot (PAPER) — {eng.date_str} ──", flush=True)
    if eng.today.weekday() >= 5:
        print("  [ndp] weekend — nothing to do.", flush=True)
        return
    try:
        eng.connect_kite()
        eng.load_contracts()
    except Exception as e:
        eng.status = "error"
        eng.event("ABORT", f"Kite unavailable: {type(e).__name__}: {e}")
        return
    eng.seed()
    eng.restore_or_start()
    eng.write_state()

    from kiteconnect import KiteTicker
    eng.kws = KiteTicker(kx._API_KEY, kx.access_token())
    eng.kws.on_ticks = eng.on_ticks
    eng.kws.on_connect = eng.on_connect
    eng.kws.on_close = eng.on_close
    eng.kws.on_error = eng.on_error

    threading.Thread(target=eng.step_loop, daemon=True, name="ndp-step").start()
    threading.Thread(target=eng.writer_loop, daemon=True, name="ndp-writer").start()
    eng.kws.connect()           # blocks; KiteTicker reconnects on its own


if __name__ == "__main__":
    main()
