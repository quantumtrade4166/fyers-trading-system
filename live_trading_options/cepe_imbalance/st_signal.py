"""
cepe_imbalance/st_signal.py — Supertrend on 1-minute ATM CE+PE candles, per underlying (owner 13-Nov-2026).
=========================================================================================================

Feeds the Supertrend VARIANT books ("{NAME}_ST", strategy CePeSupertrend):
  * price  = ATM CE + ATM PE (marks) of the variant's trading expiry; ATM follows the reference price with
             the same half-step + buffer hysteresis as the strategy. Sampled every 0.5 s -> 1-minute candles.
  * trend  = Supertrend (TradingView logic: Wilder ATR, hl2 bands, starts in a down trend) of the CLOSED
             candles. red = down trend (premium falling) -> the variant is in; green -> flat.
  * warm-up: the previous session's last ST_WARMUP_MIN minutes, REPLAYED from the recorded quotes
             (data/quotes/{day}/{NAME}.qrec) for the SAME expiry the variant trades today. So the day after
             an expiry it uses the new contracts as they traded yesterday, never the expired ones (whose
             straddle decays to ~0 and would read as a huge jump at the open). Today's recorded quotes are
             replayed too, so an engine restart mid-day keeps its candles. No recording -> today only.
  * opening rule: if Supertrend is already red at the open (state after the warm-up), the variant enters
             only once CE+PE trades below the LOW of the market's first 1-minute candle; after the first
             green candle of the day, entries/exits follow red/green closes.
"""
import datetime as dt
import logging
import threading
import time

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import quotes as Q

log = logging.getLogger("cepe.st")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SAMPLE_S = 0.5
PRE_S = 600                  # replay starts this long before the window so every price is current


def buffered_atm(ref, cur, step, buf_pct):
    """Grid ATM with hysteresis (same rule as CePeImbalance.current_atm)."""
    if not ref or ref <= 0 or not step:
        return cur
    near = round(ref / step) * step
    if cur is None:
        return near
    return near if abs(ref - cur) > step * (0.5 + buf_pct / 100.0) else cur


def supertrend(bars, per=10, mul=3.0):
    """bars [{t, o, h, l, c}] -> [(t, value, up)] from bar `per` on. TradingView ta.supertrend logic."""
    out, atr, trs, fu, fl, up = [], None, [], 0.0, 0.0, False
    for i, b in enumerate(bars):
        pc = bars[i - 1]["c"] if i else b["c"]
        tr = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
        if atr is None:
            trs.append(tr)
            if len(trs) < per:
                continue
            atr = sum(trs) / per
            first = True
        else:
            atr = (atr * (per - 1) + tr) / per
            first = False
        mid = (b["h"] + b["l"]) / 2
        bu, bl = mid + mul * atr, mid - mul * atr
        if first:
            fu, fl = bu, bl
        else:
            fu = bu if (bu < fu or pc > fu) else fu
            fl = bl if (bl > fl or pc < fl) else fl
            if up and b["c"] < fl:
                up = False
            elif not up and b["c"] > fu:
                up = True
        out.append((b["t"], round(fl if up else fu, 2), up))
    return out


class Candles:
    """0.5 s price samples -> 1-minute OHLC candles (bar t = minute start, epoch s)."""

    def __init__(self):
        self.bars: list = []          # closed
        self.cur: dict | None = None

    def add(self, t: float, px: float):
        m = int(t // 60 * 60)
        c = self.cur
        if c and c["t"] == m:
            c["h"], c["l"], c["c"] = max(c["h"], px), min(c["l"], px), px
            return
        if c:
            self.bars.append(c)
        self.cur = {"t": m, "o": px, "h": px, "l": px, "c": px}

    def close_until(self, t: float):
        """Close the running candle once its minute is over (no new sample needed)."""
        if self.cur and t >= self.cur["t"] + 60:
            self.bars.append(self.cur)
            self.cur = None


class Sampler:
    """ATM CE+PE price of one underlying + expiry from a Market (live or replay)."""

    def __init__(self, und, expiry):
        self.und, self.expiry, self.atm = und, expiry, None

    def price(self, market, step, buf):
        self.atm = buffered_atm(market.ref_price(self.und, self.expiry), self.atm, step, buf)
        if self.atm is None:
            return None
        ce = market.contract(self.und, self.expiry, self.atm, "CE")
        pe = market.contract(self.und, self.expiry, self.atm, "PE")
        a, b = (market.mark(ce.key) if ce else 0.0), (market.mark(pe.key) if pe else 0.0)
        return round(a + b, 2) if a > 0 and b > 0 else None


def _days_between(t_from, t_to, date=None) -> list:
    """Calendar (IST) dates a [t_from, t_to] window touches — a BTC full-expiry session crosses midnight."""
    d0 = dt.datetime.fromtimestamp(t_from, IST).date()
    d1 = dt.datetime.fromtimestamp(t_to, IST).date()
    out = []
    while d0 <= d1:
        out.append(d0.isoformat())
        d0 += dt.timedelta(days=1)
    return out or [date]


def replay_bars(universe, und, date, expiry, step, buf, t_from, t_to, root=None):
    """Recorded quotes of `date` -> closed 1-minute candles of the ATM CE+PE of `expiry`, between t_from
    and t_to (epoch s). Every recorded tick is applied (so prices are current), samples every 0.5 s."""
    from live_trading_options.cepe_imbalance.feed import Tick
    from live_trading_options.cepe_imbalance.market import Market
    m = Market(universe)
    ref_key = m.ref_key(und, expiry)

    def keep(key, meta):                     # only this expiry's options + the reference price
        return key == ref_key or (meta is not None and meta[1] in ("CE", "PE") and meta[2] == expiry)
    rows = (r for d in _days_between(t_from - PRE_S, t_to, date)
            for _meta, r in Q.iter_window(d, und, t_from - PRE_S, t_to, keep, folder=root))
    s = Sampler(und, expiry)
    cd = Candles()
    nxt = t_from - (t_from % SAMPLE_S)

    def sample_until(limit):
        nonlocal nxt
        while nxt < limit:
            if nxt >= t_from:
                px = s.price(m, step, buf)
                if px:
                    cd.add(nxt, px)
            nxt += SAMPLE_S

    for r in rows:
        t = r[0]
        if t >= t_to:
            break
        sample_until(t + 1e-9)                                   # samples up to this tick, before applying it
        m.apply([Tick(r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], int(t), t)])
    sample_until(t_to)                                           # prices hold after the last recorded tick
    cd.close_until(t_to)
    return [b for b in cd.bars if t_from <= b["t"] and b["t"] + 60 <= t_to]


class StSignal:
    """Live Supertrend state of one underlying for the variant book."""

    def __init__(self, und):
        self.und = und
        self.lock = threading.Lock()
        self.reset(None, None)

    def reset(self, day, expiry):
        with getattr(self, "lock", threading.Lock()):
            self.day, self.expiry = day, expiry
            self.warm: list = []                  # previous-session candles (closed)
            self.today = Candles()                # today's candles (session only)
            self.sampler = Sampler(self.und, expiry)
            self.open_ts = None
            self.px = None
            self.warm_source = "pending"
            self.warm_day = None
            self._cache = None

    # live sampling (engine timer thread, every 0.5 s)
    def sample(self, now_ts, market, step, buf, open_ts, close_ts):
        with self.lock:
            self.open_ts = open_ts
            if not (open_ts <= now_ts < close_ts):
                self.today.close_until(now_ts)
                return
            px = self.sampler.price(market, step, buf)
            self.today.close_until(now_ts)
            if px:
                self.px = px
                n = len(self.today.bars)
                self.today.add(now_ts, px)
                if len(self.today.bars) != n:
                    self._cache = None

    def install_warmup(self, warm_bars, today_bars, source, warm_day):
        """Background warm-up result: previous-session candles + today's candles up to the engine start."""
        with self.lock:
            self.warm = list(warm_bars)
            first_live = self.today.bars[0]["t"] if self.today.bars else (self.today.cur or {}).get("t")
            keep = [b for b in today_bars if first_live is None or b["t"] < first_live]
            self.today.bars = keep + self.today.bars
            self.warm_source, self.warm_day = source, warm_day
            self._cache = None

    def state(self, per=10, mul=3.0) -> dict:
        with self.lock:
            key = (len(self.warm), len(self.today.bars), per, mul, self.warm_source)
            if self._cache and self._cache[0] == key:
                st = self._cache[1]
            else:
                st = self._compute(per, mul)
                self._cache = (key, st)
            return {**st, "px": self.px, "atm": self.sampler.atm, "live_bar": dict(self.today.cur) if self.today.cur else None}

    def _compute(self, per, mul):
        bars = self.warm + self.today.bars
        series = supertrend(bars, max(1, int(per)), float(mul))
        n_warm = len(self.warm)
        first_today = self.today.bars[0] if self.today.bars and self.open_ts and \
            self.today.bars[0]["t"] == int(self.open_ts) else (self.today.bars[0] if self.today.bars else None)
        by_t = {t: (v, up) for t, v, up in series}
        # Supertrend state at the open: last warm-up candle, else the first reading of today
        start = None
        if n_warm and self.warm[-1]["t"] in by_t:
            start = by_t[self.warm[-1]["t"]][1]
        today_states = [by_t[b["t"]] for b in self.today.bars if b["t"] in by_t]
        if start is None and today_states:
            start = today_states[0][1]
        flipped = start is not None and any(up != start for _v, up in today_states)
        last = series[-1] if series else None
        return {"ready": last is not None and self.warm_source != "pending",
                "trend": None if not last else ("green" if last[2] else "red"),
                "st": last[1] if last else None, "bar_t": last[0] if last else None,
                "start_red": None if start is None else (not start), "flipped_today": flipped,
                "first_low": first_today["l"] if first_today else None,
                "first_t": first_today["t"] if first_today else None,
                "bars_today": len(self.today.bars), "warm_bars": n_warm, "warm_source": self.warm_source,
                "warm_day": self.warm_day, "expiry": self.expiry,
                "series": [(t, v, up) for t, v, up in series if t >= (self.today.bars[0]["t"] if self.today.bars else 0)]}


def previous_quote_day(und, today: str, root=None):
    base = root or (C.DATA / "quotes")
    if not base.exists():
        return None
    days = sorted(d.name for d in base.iterdir() if d.is_dir() and d.name < today and (d / f"{und}.qrec").exists())
    return days[-1] if days else None


def warm_up(universe, und, today, expiry, step, buf, session_fn, now_ts=None, root=None):
    """-> (warm_bars, today_bars, source, warm_day)."""
    now_ts = now_ts or time.time()
    warm, src, wd = [], "none", None
    if C.UNDERLYINGS[und]["kind"] == "crypto":
        # 24/7 market: warm up on the last ST_WARMUP_MIN minutes right BEFORE this session's open, same
        # expiry (the engine records the next expiries' chain all the time).
        o, _close, _sq = session_fn(und, dt.date.fromisoformat(today))
        t_to = min(o.timestamp(), now_ts)
        warm = replay_bars(universe, und, today, expiry, step, buf, t_to - C.ST_WARMUP_MIN * 60, t_to, root)
        src, wd = ("replay before open" if warm else "none"), (today if warm else None)
        pd = None
    else:
        pd = previous_quote_day(und, today, root)
    if pd:
        o, close, _sq = session_fn(und, dt.date.fromisoformat(pd))
        t_to = close.timestamp()
        t_from = max(o.timestamp(), t_to - C.ST_WARMUP_MIN * 60)
        warm = replay_bars(universe, und, pd, expiry, step, buf, t_from, t_to, root)
        src, wd = (f"replay {pd}" if warm else "none"), (pd if warm else None)
    o, _close, _sq = session_fn(und, dt.date.fromisoformat(today))
    today_bars = []
    if now_ts > o.timestamp() + 60:
        today_bars = replay_bars(universe, und, today, expiry, step, buf, o.timestamp(), now_ts, root)
    return warm, today_bars, src, wd


# ── warm-up in its own process ──────────────────────────────────────────────
def universe_from_cache(day: str):
    """Today's universe from the scrip masters the engine already cached (no broker login)."""
    from live_trading_options.cepe_imbalance import instruments as I
    from live_trading_options.cepe_imbalance import crypto
    texts = {}
    for seg in C.KOTAK_SEGS:
        p = C.DATA / f"scrip_{seg}_{day}.csv"
        if p.exists():
            texts[seg] = p.read_text(encoding="utf-8")
    u = I.build(texts, today=day)
    crypto.attach(u, crypto.cached_products(), keep_settled=True)   # BTC: the engine's own products cache
    return u


def warm_up_subprocess(und, day, expiry, step, buf, timeout=600):
    """Run warm_up() in a separate below-normal-priority process: replaying a day of quotes is CPU-heavy
    Python and must never hold the engine's GIL while the feed is live (mid-day restart)."""
    import json
    import os
    import subprocess
    import sys
    flags = (0x00004000 | getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    root = C.HERE.parents[1]
    r = subprocess.run([sys.executable, "-m", "live_trading_options.cepe_imbalance.st_signal",
                        und, day, expiry, str(step), str(buf)], capture_output=True, text=True,
                       timeout=timeout, creationflags=flags, cwd=str(root))
    if r.returncode != 0:
        raise RuntimeError(f"warm-up exit {r.returncode}: {(r.stderr or r.stdout)[-400:]}")
    d = json.loads(r.stdout.strip().splitlines()[-1])
    return d["warm"], d["today"], d["source"], d["warm_day"]


if __name__ == "__main__":
    import json
    import sys
    a = sys.argv[1:]
    und, day, expiry, step, buf = a[0], a[1], a[2], float(a[3]), float(a[4])
    from live_trading_options.cepe_imbalance.engine import session
    t = time.time()
    w, tb, src, wd = warm_up(universe_from_cache(day), und, day, expiry, step, buf, session)
    print(json.dumps({"warm": w, "today": tb, "source": src, "warm_day": wd, "secs": round(time.time() - t, 1)}))
