import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Book #11 — Anna Coulling, "A Complete Guide to Volume Price Analysis"
Strategies 11.1 – 11.14, as objective rules.

Every function is LONG-only:  fn(df, cfg) -> signals
(entry = 1, exit_long, stop_long). The engine builds the short side by
running the same function on price-mirrored features (engine/universe.py
combine_sides), so both sides always share identical rules.

df carries the columns from engine/vpa.py add_vpa_features().
Rules + book references: vault note "Coulling VPA Strategy Catalog — 11.x".
Every threshold below is a parameter — the book gives none.
"""

import warnings

import numpy as np
import pandas as pd

from backtesting.engine.harness import blank_signals
from backtesting.engine.vpa import last_pivots

# Coulling's volume buckets (relative volume): low < 0.75 < average < 1.25
# < above < 1.75 < high < 2.5 < ultra.   Spread: wide > 1.3x, narrow < 0.7x.
LOW, ABOVE, HIGH = 0.75, 1.25, 1.75
WIDE, NARROW = 1.3, 0.7


def _a(df, col):
    return df[col].to_numpy(dtype=float)


def _b(df, col):
    return df[col].fillna(False).to_numpy(dtype=bool)


def _intraday(df):
    """Session rules apply? Set by vpa.add_vpa_features (NSE intraday = yes,
    daily or 24/7 crypto = no)."""
    return bool(df.attrs.get("sessions", False))


def _sess(df):
    """Session id per bar: the date when intraday, a constant for daily data
    (so "same session" checks never block a daily sequence)."""
    return df["day"].to_numpy() if _intraday(df) else np.zeros(len(df), int)


def _same(df, k):
    """Bar and the bar k back are in the same session (always True daily)."""
    if not _intraday(df):
        return pd.Series(True, index=df.index)
    return df["day"] == df["day"].shift(k)


def _anomaly_exit(df):
    """Coulling's exit for a long: an up candle that is an anomaly —
    wide spread on low volume (no effort) or narrow spread on high volume
    (effort, no result) — or a high-volume shooting star."""
    up = df["up"]
    return ((up & (df["body_rel"] >= WIDE) & (df["rv"] < LOW))
            | (up & (df["body_rel"] <= NARROW) & (df["rv"] >= HIGH))
            | (df["shooting_star"] & (df["rv"] >= HIGH)))


# ── 11.1 Validated Trend Join ────────────────────────────────────────────────
def s11_1(df, cfg):
    """3 up candles, each wider and on more volume than the last (Fig 4.14)."""
    sig = blank_signals(df)
    up, b, r = df["up"], df["body_rel"], df["rv"]
    cond = (up & up.shift(1) & up.shift(2)
            & (b > b.shift(1)) & (b.shift(1) > b.shift(2))
            & (r > r.shift(1)) & (r.shift(1) > r.shift(2))
            & (r >= cfg.get("rv_min", 1.0))
            & (df["upper_wick"] <= cfg.get("max_wick", 0.25)))
    cond &= _same(df, 2)                       # all 3 bars in one session
    sig["entry"] = cond.astype(int)
    sig["stop_long"] = df["low"].shift(2)
    sig["exit_long"] = _anomaly_exit(df)
    return sig


# ── 11.2 No-Effort Trap Fade ─────────────────────────────────────────────────
def s11_2(df, cfg):
    """Wide-spread DOWN candle on low volume = trap move -> buy (Fig 4.12).
    The mirror gives the book's headline case: wide UP candle, low vol -> sell."""
    sig = blank_signals(df)
    cond = (df["down"] & (df["body_rel"] >= cfg.get("body_min", 1.5))
            & (df["rv"] < cfg.get("rv_max", LOW))
            & (df["close_pos"] <= 0.3))
    if cfg.get("open_only"):
        cond &= df.index.time <= pd.Timestamp("09:30").time()
    sig["entry"] = cond.astype(int)
    sig["stop_long"] = df["low"] - 0.1 * df["atr"]
    return sig


# ── 11.3 Absorption Reversal (effort, no result) ─────────────────────────────
def s11_3(df, cfg):
    """After a DOWN-trend, a narrow DOWN candle on high volume = buying
    absorbing the selling (Fig 4.17). Buy when price closes above that
    candle's high within `window` bars; optional confirmation that the bars
    in between fell on LOWER volume (Fig 4.17 candles 3-4)."""
    sig = blank_signals(df)
    n = len(df)
    mom, down, b, r = _a(df, "mom"), _b(df, "down"), _a(df, "body_rel"), _a(df, "rv")
    hi, lo, cl = _a(df, "high"), _a(df, "low"), _a(df, "close")
    day = _sess(df)
    k, rvmin, win = cfg.get("trend", 1.5), cfg.get("rv_min", HIGH), cfg.get("window", 6)
    confirm = cfg.get("confirm", True)
    entry = np.zeros(n, int); stop = np.full(n, np.nan)
    a_i = -1
    for i in range(n):
        if a_i >= 0 and (i - a_i > win or day[i] != day[a_i]):
            a_i = -1
        if a_i >= 0 and i > a_i and cl[i] > hi[a_i]:
            ok = True
            if confirm:
                mid = r[a_i + 1:i]
                ok = len(mid) > 0 and np.all(mid < r[a_i])
            if ok:
                entry[i] = 1
                stop[i] = np.nanmin(lo[a_i:i + 1])
            a_i = -1
            continue
        if (down[i] and mom[i] <= -k and b[i] <= NARROW and r[i] >= rvmin):
            a_i = i
    sig["entry"] = entry; sig["stop_long"] = stop
    sig["exit_long"] = _anomaly_exit(df)
    return sig


# ── 11.4 Double Hammer (mirror = Double Shooting Star) ──────────────────────
def s11_4(df, cfg):
    """Down-trend -> hammer on >= above-avg volume, then within 5 bars a 2nd
    hammer on HIGHER volume with a HIGHER low -> buy (AUD/USD Fig 10.16
    mirrored). Stop below the first hammer's low. Exit on topping-out
    weakness (anomaly)."""
    sig = blank_signals(df)
    n = len(df)
    ham, r, mom = _b(df, "hammer"), _a(df, "rv"), _a(df, "mom")
    lo = _a(df, "low"); day = _sess(df)
    k, win = cfg.get("trend", 1.0), cfg.get("window", 5)
    entry = np.zeros(n, int); stop = np.full(n, np.nan)
    h1 = -1
    for i in range(n):
        if h1 >= 0 and (i - h1 > win or day[i] != day[h1]):
            h1 = -1
        if ham[i] and r[i] >= ABOVE:
            if h1 >= 0 and r[i] > r[h1] and lo[i] > lo[h1]:
                entry[i] = 1; stop[i] = lo[h1]; h1 = -1; continue
            if mom[i] <= -k:
                h1 = i
    sig["entry"] = entry; sig["stop_long"] = stop
    sig["exit_long"] = _anomaly_exit(df)
    return sig


# ── 11.5 Stopping Volume Reversal (mirror = Topping-Out Volume) ─────────────
def s11_5(df, cfg):
    """Price waterfall, then >=`n_stop` candles with deep lower wicks on high
    volume closing in their upper half, ending in a hammer (Fig 6.17).
    Buy when price closes above the hammer's high; stop below the lowest wick."""
    sig = blank_signals(df)
    lw, cp, r = df["lower_wick"], df["close_pos"], df["rv"]
    stopping = (lw >= 0.35) & (cp >= 0.5) & (r >= cfg.get("rv_min", HIGH))
    n_stop = cfg.get("n_stop", 2)
    count = stopping.astype(int).rolling(3, min_periods=1).sum()
    waterfall = df["mom"].shift(3) <= -cfg.get("trend", 2.0)
    setup = df["hammer"] & (count >= n_stop) & waterfall
    setup &= _same(df, 3)                      # setup bars in one session
    n = len(df); hi, cl, lo = _a(df, "high"), _a(df, "close"), _a(df, "low")
    st = setup.to_numpy(); day = _sess(df)
    entry = np.zeros(n, int); stop = np.full(n, np.nan)
    s_i, win = -1, cfg.get("window", 4)
    for i in range(n):
        if s_i >= 0 and (i - s_i > win or day[i] != day[s_i]):
            s_i = -1
        if s_i >= 0 and i > s_i and cl[i] > hi[s_i]:
            entry[i] = 1; stop[i] = np.nanmin(lo[max(0, s_i - 3):i + 1]); s_i = -1
            continue
        if st[i]:
            s_i = i
    sig["entry"] = entry; sig["stop_long"] = stop
    sig["exit_long"] = _anomaly_exit(df)
    return sig


# ── 11.6 Low-Volume Test Entry ───────────────────────────────────────────────
def s11_6(df, cfg):
    """After a stopping-volume climax (11.5 setup) within `lookback` bars, a
    narrow-body candle with a deep lower wick on LOW volume that holds above
    the climax low = successful test of supply (Fig 5.12). Buy on a close
    above the test's high. A high-volume test cancels the setup (Fig 5.13)."""
    sig = blank_signals(df)
    lw, cp, r = df["lower_wick"], df["close_pos"], df["rv"]
    stopping = (lw >= 0.35) & (cp >= 0.5) & (r >= HIGH)
    climax = (df["hammer"] & (stopping.astype(int).rolling(3, min_periods=1).sum() >= 2)
              & (df["mom"].shift(3) <= -cfg.get("trend", 2.0)))
    n = len(df)
    cm, lwv, bf, rv = climax.to_numpy(), _a(df, "lower_wick"), _a(df, "body_frac"), _a(df, "rv")
    hi, lo, cl = _a(df, "high"), _a(df, "low"), _a(df, "close")
    day = _sess(df)
    lb, rvmax = cfg.get("lookback", 30), cfg.get("rv_max", LOW)
    entry = np.zeros(n, int); stop = np.full(n, np.nan)
    c_i, c_low, t_i = -1, np.nan, -1
    for i in range(n):
        if c_i >= 0 and (i - c_i > lb or day[i] != day[c_i] or lo[i] < c_low):
            c_i, t_i = -1, -1
        if t_i >= 0 and i - t_i > 3:
            t_i = -1
        if t_i >= 0 and i > t_i and cl[i] > hi[t_i]:
            entry[i] = 1; stop[i] = lo[t_i]; c_i, t_i = -1, -1; continue
        if c_i >= 0 and i - c_i >= 2 and lwv[i] >= 0.4 and bf[i] <= 0.4:
            if rv[i] <= rvmax:
                t_i = i
            elif rv[i] >= HIGH:          # failed test: supply still there
                c_i, t_i = -1, -1
        if cm[i]:
            c_i, c_low, t_i = i, lo[i], -1
    sig["entry"] = entry; sig["stop_long"] = stop
    sig["exit_long"] = _anomaly_exit(df)
    return sig


# ── congestion zones (shared by 11.7 / 11.8 / 11.9) ─────────────────────────
def _zone(df, cfg):
    """Congestion zone as the book builds it (Ch 7, Fig 7.13–7.15, Ch 11):
    the first pivot high + pivot low after a reset set the ceiling and floor.
    Later pivots within `tol` ATR of a line REINFORCE it (the lines are
    "rubber bands", widened to the outermost pivot) instead of restarting
    the zone. The zone ends when a close breaks out by `clear` ATR, when a
    pivot forms well outside it, or when it grows taller than `max_h` ATR.

    Returned arrays describe the zone as of the END OF THE PREVIOUS BAR, so
    the current bar is judged against levels that were already known.
    valid = a zone exists, is >= `min_bars` old and began this session."""
    n = len(df)
    ph, pl = _b(df, "ph"), _b(df, "pl")
    phl, pll = _a(df, "ph_level"), _a(df, "pl_level")
    cl, atr = _a(df, "close"), _a(df, "atr")
    day = _sess(df)
    tol, clear = cfg.get("tol", 0.5), cfg.get("clear", 0.2)
    max_h, min_bars = cfg.get("max_h", 3.0), cfg.get("min_bars", 12)
    C = np.full(n, np.nan); F = np.full(n, np.nan); ST = np.full(n, np.nan)
    ceil = floor = np.nan; start = -1
    p_hi = p_lo = None                      # pending pivots (level, bar) while no zone
    for i in range(n):
        a = atr[i] if atr[i] == atr[i] else 0.0
        if start >= 0 and (cl[i] > ceil + clear * a or cl[i] < floor - clear * a
                           or day[i] != day[start]):
            start = -1; p_hi = p_lo = None
        if ph[i]:
            lv = phl[i]
            if start >= 0:
                if lv <= ceil + tol * a:
                    ceil = max(ceil, lv)
                else:                       # a pivot clearly above: zone obsolete
                    start = -1; p_hi, p_lo = (lv, i - 1), None
            else:
                p_hi = (lv, i - 1)
        if pl[i]:
            lv = pll[i]
            if start >= 0:
                if lv >= floor - tol * a:
                    floor = min(floor, lv)
                else:
                    start = -1; p_lo, p_hi = (lv, i - 1), None
            else:
                p_lo = (lv, i - 1)
        if start < 0 and p_hi and p_lo and p_hi[0] > p_lo[0] and day[p_hi[1]] == day[p_lo[1]] == day[i]:
            ceil, floor, start = p_hi[0], p_lo[0], min(p_hi[1], p_lo[1])
            p_hi = p_lo = None
        if start >= 0 and a > 0 and (ceil - floor) > max_h * a:
            start = -1
        if start >= 0:
            C[i], F[i], ST[i] = ceil, floor, start
    # shift: bar i is judged against the zone known at the close of bar i-1
    C, F, ST = np.r_[np.nan, C[:-1]], np.r_[np.nan, F[:-1]], np.r_[np.nan, ST[:-1]]
    idx = np.arange(n)
    valid = ~np.isnan(C) & (idx - np.nan_to_num(ST, nan=1e18) >= min_bars)
    return C, F, valid, last_pivots(df)


def _first_close_above(df, level, clear):
    atr, cl = _a(df, "atr"), _a(df, "close")
    above = cl > level + clear * atr
    prev = np.r_[False, (cl[:-1] > level[1:] + clear * atr[1:])]
    return above & ~prev


# ── 11.7 Volume-Confirmed Congestion Breakout ────────────────────────────────
def s11_7(df, cfg):
    """First close above the ceiling by `clear` ATR ("clear water") on
    rising, >= `rv_min` volume -> buy; stop at the floor (Fig 7.16)."""
    sig = blank_signals(df)
    ceil, floor, valid, _ = _zone(df, cfg)
    brk = _first_close_above(df, ceil, cfg.get("clear", 0.2))
    r = _a(df, "rv")
    vol_ok = (r >= cfg.get("rv_min", 1.5)) & (r > np.r_[np.nan, r[:-1]])
    sig["entry"] = (valid & brk & vol_ok).astype(int)
    sig["stop_long"] = floor
    sig["exit_long"] = _anomaly_exit(df)
    return sig


# ── 11.8 Low-Volume Fake-Out Fade ────────────────────────────────────────────
def s11_8(df, cfg):
    """First close BELOW the floor on low volume = fake-out -> buy, target the
    ceiling (mirror: fake breakout above the ceiling -> sell)."""
    sig = blank_signals(df)
    ceil, floor, valid, _ = _zone(df, cfg)
    cl, lo, atr = _a(df, "close"), _a(df, "low"), _a(df, "atr")
    below = cl < floor - cfg.get("clear", 0.2) * atr
    prev = np.r_[False, cl[:-1] < floor[1:] - cfg.get("clear", 0.2) * atr[1:]]
    cond = valid & below & ~prev & (_a(df, "rv") < cfg.get("rv_max", 1.0))
    sig["entry"] = cond.astype(int)
    sig["stop_long"] = lo - 0.1 * atr
    target = pd.Series(np.where(cond, ceil, np.nan), index=df.index).ffill()
    sig["exit_long"] = df["close"] >= target
    return sig


# ── 11.9 Dynamic Pivot Trend Hold (11.7 entry + pivot trail) ────────────────
def s11_9(df, cfg):
    """11.7's entry, then hold while pivot lows keep rising: the stop trails
    to each new confirmed pivot low (Fig 8.11–8.14). Needs harness
    trailing=True (set in the run config)."""
    sig = s11_7(df, cfg)
    p = last_pivots(df)
    sig["stop_long"] = np.where(sig["entry"] > 0, sig["stop_long"], p["pl_last"])
    sig["exit_long"] = False          # the trail and the session close do the exiting
    return sig


# ── 11.10 Inverted Hammer + Hammer Bottom (mirror = Hanging Man + Star top) ─
def s11_10(df, cfg):
    """Mirror of the book's top pattern (Fig 6.16): in a down-trend an
    inverted-hammer shape (shooting-star shape) on >= above-avg volume,
    then within 5 bars a hammer on >= above-avg volume -> buy. On the
    mirrored chart this is exactly hanging man + shooting star -> sell."""
    sig = blank_signals(df)
    n = len(df)
    star, ham, r, mom = _b(df, "shooting_star"), _b(df, "hammer"), _a(df, "rv"), _a(df, "mom")
    lo = _a(df, "low"); day = _sess(df)
    entry = np.zeros(n, int); stop = np.full(n, np.nan)
    a_i, win = -1, cfg.get("window", 5)
    for i in range(n):
        if a_i >= 0 and (i - a_i > win or day[i] != day[a_i]):
            a_i = -1
        if a_i >= 0 and ham[i] and r[i] >= ABOVE:
            entry[i] = 1; stop[i] = min(lo[a_i], lo[i]); a_i = -1; continue
        if star[i] and r[i] >= ABOVE and mom[i] <= -cfg.get("trend", 1.0):
            a_i = i
    sig["entry"] = entry; sig["stop_long"] = stop
    sig["exit_long"] = _anomaly_exit(df)
    return sig


# ── 11.11 Gap Validation ─────────────────────────────────────────────────────
def s11_11(df, cfg):
    """Opening gap UP of >= `gap` (fraction) on the first bar of the session.
    mode "follow": high volume (>= rv_hi) = genuine -> buy.
    mode "fade":   on the mirrored chart a gap DOWN on low volume -> buy,
                   i.e. a low-volume gap up -> sell (the book's trap)."""
    sig = blank_signals(df)
    day = df["day"]
    first = (day != day.shift(1)).to_numpy()
    prev_close = df["close"].shift(1).to_numpy()
    op, r, lo, hi = _a(df, "open"), _a(df, "rv"), _a(df, "low"), _a(df, "high")
    gap = (op - prev_close) / np.abs(prev_close)
    g = cfg.get("gap", 0.01)
    sane = np.abs(gap) <= cfg.get("gap_max", 0.2)   # bigger = split/bonus artefact
    first = first & sane
    if cfg.get("mode", "follow") == "follow":
        cond = first & (gap >= g) & (r >= cfg.get("rv_hi", 1.5))
    else:
        cond = first & (gap <= -g) & (r <= cfg.get("rv_lo", LOW))
    sig["entry"] = cond.astype(int)
    sig["stop_long"] = np.where(cond, lo - 0.1 * _a(df, "atr"), np.nan)
    return sig


# ── 11.12 Trend Pullback on Falling Volume ───────────────────────────────────
def s11_12(df, cfg):
    """Up-trend by dynamic pivots (higher pivot high AND higher pivot low).
    A pullback of >= 2 down bars on falling, sub-`rv_pb` volume (no stopping
    volume), then an up bar on rising volume closing above the prior high -> buy."""
    sig = blank_signals(df)
    p = last_pivots(df)
    trend = (p["ph_last"] > p["ph_prev"]) & (p["pl_last"] > p["pl_prev"])
    d, r = df["down"], df["rv"]
    pb = (d.shift(1) & d.shift(2) & (r.shift(1) < r.shift(2))
          & (r.shift(1) < cfg.get("rv_pb", 1.0)) & (r.shift(2) < cfg.get("rv_pb", 1.0)))
    go = df["up"] & (r > r.shift(1)) & (df["close"] > df["high"].shift(1))
    cond = trend & pb & go & _same(df, 2)
    sig["entry"] = cond.astype(int)
    sig["stop_long"] = pd.concat([df["low"], df["low"].shift(1), df["low"].shift(2)], axis=1).min(axis=1)
    sig["exit_long"] = _anomaly_exit(df)
    return sig


# ── 11.13 Rising Triangle / Triple Bottom ────────────────────────────────────
def s11_13(df, cfg):
    """pattern "triangle": two pivot highs at the same level (within `tol`
    ATR) with a higher pivot low under them; buy the volume-confirmed close
    above that ceiling (Fig 11.14). Mirror = falling triangle.
    pattern "triple": three pivot lows within `tol` ATR, then a hammer or
    up candle on >= above-avg volume near that floor -> buy (Fig 11.17).
    Mirror = triple top."""
    sig = blank_signals(df)
    atr, cl, lo = _a(df, "atr"), _a(df, "close"), _a(df, "low")
    tol = cfg.get("tol", 0.3)
    p = last_pivots(df)
    if cfg.get("pattern", "triangle") == "triangle":
        ph, php = p["ph_last"].to_numpy(), p["ph_prev"].to_numpy()
        flat = np.abs(ph - php) <= tol * atr
        rising = (p["pl_last"] > p["pl_prev"]).to_numpy()
        ceil = np.fmax(ph, php)
        brk = _first_close_above(df, ceil, cfg.get("clear", 0.2))
        r = _a(df, "rv")
        cond = flat & rising & brk & (r >= cfg.get("rv_min", 1.5))
        sig["stop_long"] = p["pl_last"].to_numpy()
    else:
        lv = df["pl_level"].dropna()
        l1 = lv.reindex(df.index).ffill().to_numpy()
        l2 = lv.shift(1).reindex(df.index).ffill().to_numpy()
        l3 = lv.shift(2).reindex(df.index).ffill().to_numpy()
        lvls = np.vstack([l1, l2, l3])
        with warnings.catch_warnings():       # early bars have <3 pivots yet
            warnings.simplefilter("ignore", RuntimeWarning)
            tight = (np.nanmax(lvls, 0) - np.nanmin(lvls, 0)) <= tol * atr
            floor = np.nanmin(lvls, 0)
        near = lo <= floor + 0.5 * atr
        bounce = (_b(df, "hammer") | _b(df, "up")) & (_a(df, "rv") >= ABOVE)
        cond = tight & near & bounce & (cl > floor)
        sig["stop_long"] = floor - 0.2 * atr
    sig["entry"] = np.nan_to_num(cond).astype(int)
    sig["exit_long"] = _anomaly_exit(df)
    return sig


# ── 11.14 Volume-at-Price Value-Area Breakout ────────────────────────────────
def s11_14(df, cfg):
    """Volume-at-price over the previous `days` sessions (turnover by close
    price, `bins` buckets) -> point of control (POC) and 70% value area.
    A first close above the value-area high on >= `rv_min` volume -> buy,
    stop at the POC (Ch 9: high-volume nodes are the floors/ceilings).
    Levels for a day use ONLY earlier days."""
    sig = blank_signals(df)
    nd, bins = cfg.get("days", 5), cfg.get("bins", 40)
    day = df["day"]
    days = day.unique()
    cl = df["close"].to_numpy(); to = (df["volume"].to_numpy(float) * np.abs(cl))
    dpos = {d: k for k, d in enumerate(days)}
    di = day.map(dpos).to_numpy()
    vah = np.full(len(df), np.nan); poc = np.full(len(df), np.nan)
    for k in range(nd, len(days)):
        m = (di >= k - nd) & (di < k)
        px, w = cl[m], to[m]
        if len(px) < 10:
            continue
        hist, edges = np.histogram(px, bins=bins, weights=w)
        mid = (edges[:-1] + edges[1:]) / 2
        pk = int(np.argmax(hist))
        lo_i = hi_i = pk; tot = hist[pk]; target = 0.7 * hist.sum()
        while tot < target and (lo_i > 0 or hi_i < bins - 1):
            nxt_lo = hist[lo_i - 1] if lo_i > 0 else -1
            nxt_hi = hist[hi_i + 1] if hi_i < bins - 1 else -1
            if nxt_hi >= nxt_lo:
                hi_i += 1; tot += hist[hi_i]
            else:
                lo_i -= 1; tot += hist[lo_i]
        today = di == k
        vah[today] = edges[hi_i + 1]; poc[today] = mid[pk]
    brk = _first_close_above(df, vah, cfg.get("clear", 0.1))
    cond = brk & (_a(df, "rv") >= cfg.get("rv_min", 1.5)) & (poc < cl)
    sig["entry"] = cond.astype(int)
    sig["stop_long"] = poc
    sig["exit_long"] = _anomaly_exit(df)
    return sig
