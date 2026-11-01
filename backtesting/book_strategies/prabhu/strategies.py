import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Book #1 — Vikram Prabhu, "25 Day Trading Strategies in Nifty & Bank Nifty"
Strategies 1.1 – 1.25 as objective rules (5-minute bars, intraday).

Every function is LONG-only:  fn(df, cfg) -> signals
(entry, stop_long, target_long). The engine builds the short side by running
the same function on a price-mirrored chart (engine/universe.combine_sides).
Book pairs share one function: 1.2/1.3, 1.11/1.12, 1.13/1.14, 1.16/1.17,
1.19/1.20 (long side = the first ID or the second — see the runner's map).

df needs engine/vpa.add_vpa_features + engine/levels.add_levels columns.
Rules + page references: vault "Book 1 Strategy Catalog — 1.x".

Common cfg keys
  sl      "candle" (default) | "level"   — the book's two stop choices
  tgt     "2R" (default)     | "level"   — 2R, or the level the book names
                                           (harness falls back to 2R if that
                                           level isn't beyond the entry)
  daytype None | "trend" | "side"        — CPR-width filter (Ch 4)
  max_per_day  default 2                 — book's "max 2 trades per day"
  dtrend  True -> F2 daily-trend filter (yesterday's close vs daily 20 EMA)
  agree   True -> F4 peer index also up on the day (needs peer_ret column)
"""

import numpy as np
import pandas as pd

from backtesting.engine.harness import blank_signals

BUF = 0.05          # stop buffer beyond a level / candle, in ATR
BIG = 2.0           # "big" candle: range >= BIG x ATR
NEAR = 0.25         # "at" a level: within NEAR x ATR


def _a(df, c):
    return df[c].to_numpy(dtype=float)


def _cross_up(df, level):
    """Close above `level` this bar, at/below it on the previous bar (same session)."""
    cl = _a(df, "close")
    lv = np.asarray(level, dtype=float)
    prev_cl = np.r_[np.nan, cl[:-1]]
    prev_lv = np.r_[np.nan, lv[:-1]]
    same = (df["k"].to_numpy() > 0)
    return (cl > lv) & (prev_cl <= prev_lv) & same


def _next_above(df, cols):
    cl = _a(df, "close")
    m = np.vstack([_a(df, c) for c in cols])
    m = np.where(m > cl, m, np.nan)
    with np.errstate(all="ignore"):
        return np.where(np.all(np.isnan(m), 0), np.nan, np.nanmin(np.where(np.isnan(m), np.inf, m), 0))


def _finish(df, cfg, cond, stop_candle, stop_level, tgt_level=None):
    """Apply day-type filter, max-per-day, stop/target choice."""
    sig = blank_signals(df)
    cond = np.nan_to_num(np.asarray(cond, dtype=float)).astype(bool)
    rank = _a(df, "cpr_rank")
    # F2 daily trend (Ch 6): long only if yesterday closed above the daily 20 EMA
    if cfg.get("dtrend"):
        cond &= _a(df, "pdc") > _a(df, "d_ema20")
    # F4 Nifty-Bank Nifty agreement (p35): the peer index is also up on the day
    if cfg.get("agree") and "peer_ret" in df:
        cond &= _a(df, "peer_ret") > 0
    dt = cfg.get("daytype")
    if dt == "trend":
        cond &= ~(rank > 0.7)
    elif dt == "side":
        cond &= ~(rank < 0.3)
    s = pd.Series(cond, index=df.index)
    nth = s.astype(int).groupby(df["day"]).cumsum()
    cond = (s & (nth <= cfg.get("max_per_day", 2))).to_numpy()
    atr = _a(df, "atr")
    stop = np.asarray(stop_level if cfg.get("sl") == "level" else stop_candle, dtype=float) - BUF * atr
    sig["entry"] = cond.astype(int)
    sig["stop_long"] = np.where(cond, stop, np.nan)
    tl = np.full(len(df), np.nan)
    if cfg.get("tgt") == "level" and tgt_level is not None:
        tl = np.where(cond, np.asarray(tgt_level, dtype=float), np.nan)
    sig["target_long"] = tl
    return sig


# ───────────────────────────── Trending days (Ch 7) ─────────────────────────

def od(df, cfg):
    """1.1 Open Drive: open above PDH, bullish first candle. entry=1 on its
    close; entry=2 when its high is broken. SL first-candle low / PDH.
    Target next pivot or 2R."""
    k, op, cl, hi = df["k"].to_numpy(), _a(df, "open"), _a(df, "close"), _a(df, "high")
    first_bull = (_a(df, "fc_c") > _a(df, "fc_o")) & (_a(df, "d_open") > _a(df, "pdh"))
    if cfg.get("pin"):     # book: "first 5 min PIN BAR candle" — long lower wick
        rng = (_a(df, "fc_h") - _a(df, "fc_l"))
        lw = np.minimum(_a(df, "fc_o"), _a(df, "fc_c")) - _a(df, "fc_l")
        first_bull &= lw >= 0.5 * np.where(rng > 0, rng, np.nan)
    if cfg.get("entry", 1) == 1:
        cond = first_bull & (k == 0)
    else:
        cond = first_bull & _cross_up(df, _a(df, "fc_h"))
    return _finish(df, cfg, cond, _a(df, "fc_l"), _a(df, "pdh"),
                   _next_above(df, ["r1", "r2", "r3"]))


def star_reclaim(df, cfg):
    """1.2 Morning Star (long) / 1.3 Evening Star (mirror): price has been
    below PDL today, then a candle closes back ABOVE PDL. SL entry candle /
    PDL. Target CPR or 2R."""
    below = _a(df, "lo_prev") < _a(df, "pdl")
    cond = below & _cross_up(df, _a(df, "pdl"))
    return _finish(df, cfg, cond, _a(df, "low"), _a(df, "pdl"), _a(df, "cpr_bot"))


def cprbo(df, cfg):
    """1.4 CPR Breakout: approaching from below, a green candle closes above
    the CPR top. SL entry candle / central pivot. Target next resistance or 2R."""
    green = _a(df, "close") > _a(df, "open")
    from_below = _a(df, "d_open") < _a(df, "cpr_top")
    cond = green & from_below & _cross_up(df, _a(df, "cpr_top"))
    return _finish(df, cfg, cond, _a(df, "low"), _a(df, "pp"),
                   _next_above(df, ["r1", "r2", "pdh"]))


def zone_break(df, cfg):
    """1.5 Supply Zone Breakout: close above the nearest supply zone (big crash
    origin, last 3 sessions). SL entry candle / zone line. 2R."""
    green = _a(df, "close") > _a(df, "open")
    cond = green & _cross_up(df, _a(df, "sz"))
    return _finish(df, cfg, cond, _a(df, "low"), _a(df, "sz"))


def orb(df, cfg):
    """1.6 Opening Range Breakout: close above the opening-range high
    (or_bars set in levels; 6 = 30 min). SL entry candle / range high. 2R."""
    hcol = "or3_h" if cfg.get("or_min", 30) == 15 else "or_h"
    green = _a(df, "close") > _a(df, "open")
    cond = green & _cross_up(df, _a(df, hcol))
    return _finish(df, cfg, cond, _a(df, "low"), _a(df, hcol))


def dayhigh_break(df, cfg):
    """1.7 Day High Breakout: the day's high is >= 3 bars old and price pulled
    back >= 1 ATR below it; then a green candle closes above it. SL entry
    candle / the breakout line. 2R."""
    n = len(df)
    hi, lo, cl, op, atr = (_a(df, c) for c in ("high", "low", "close", "open", "atr"))
    k = df["k"].to_numpy()
    cond = np.zeros(n, bool); line = np.full(n, np.nan)
    dh = np.nan; dh_bar = -1; low_since = np.nan
    for i in range(n):
        if k[i] == 0:
            dh, dh_bar, low_since = hi[i], i, lo[i]
            continue
        if (i - dh_bar >= 3 and dh - low_since >= atr[i] and cl[i] > dh
                and cl[i - 1] <= dh and cl[i] > op[i]):
            cond[i], line[i] = True, dh
        if hi[i] > dh:
            dh, dh_bar, low_since = hi[i], i, lo[i]
        else:
            low_since = min(low_since, lo[i])
    return _finish(df, cfg, cond, lo, line)


def gap_fill(df, cfg):
    """1.8 Gap Filling: price breaks above the near border of an unfilled daily
    gap above -> trade into the gap. SL candle / border. Target far border or 2R."""
    green = _a(df, "close") > _a(df, "open")
    cond = green & _cross_up(df, _a(df, "gz_lo"))
    return _finish(df, cfg, cond, _a(df, "low"), _a(df, "gz_lo"), _a(df, "gz_hi"))


def ma_trend(df, cfg):
    """1.9 MA Trend Following: 20 EMA sloping up; a bullish candle that dips to
    the 8 EMA (ema=8, aggressive) or the 20 EMA (ema=20) and closes above it.
    SL entry candle / 20 EMA. 2R."""
    e = _a(df, f"ema{cfg.get('ema', 20)}")
    e20 = _a(df, "ema20")
    rising = e20 > np.r_[np.full(5, np.nan), e20[:-5]]
    green = _a(df, "close") > _a(df, "open")
    cond = rising & green & (_a(df, "low") <= e) & (_a(df, "close") > e) & (df["k"].to_numpy() >= 1)
    return _finish(df, cfg, cond, _a(df, "low"), e20)


# ───────────────────────────── Sideways days (Ch 8) ─────────────────────────

def ppt(df, cfg):
    """1.10 PPT: first candle green, opens at/above CPR bottom and closes above
    the CPR top -> long on its close. SL first candle / CPR. Target PDH or 2R."""
    k = df["k"].to_numpy()
    cond = ((k == 0) & (_a(df, "fc_c") > _a(df, "fc_o"))
            & (_a(df, "fc_o") >= _a(df, "cpr_bot")) & (_a(df, "fc_c") > _a(df, "cpr_top")))
    return _finish(df, cfg, cond, _a(df, "fc_l"), _a(df, "cpr_bot"), _a(df, "pdh"))


def retrace(df, cfg):
    """1.11 GCR (long) / 1.12 RCR (mirror): big green first candle; price
    retraces back to within NEAR ATR of its low; a bullish candle there -> long.
    SL below first candle / entry candle. Target first-candle high or 2R."""
    big = (_a(df, "fc_h") - _a(df, "fc_l")) >= BIG * _a(df, "atr")
    green_fc = _a(df, "fc_c") > _a(df, "fc_o")
    near = _a(df, "low") <= _a(df, "fc_l") + NEAR * _a(df, "atr")
    held = _a(df, "close") > _a(df, "fc_l")
    bull = _a(df, "close") > _a(df, "open")
    cond = big & green_fc & near & held & bull & (df["k"].to_numpy() >= 2)
    return _finish(df, cfg, cond, _a(df, "low"), _a(df, "fc_l"), _a(df, "fc_h"))


def double_test(df, cfg):
    """1.14 W (long) / 1.13 M (mirror): after the day's low was set, price
    bounced >= `bounce` ATR above it (demand zone); price then comes back to
    within NEAR ATR of that low and prints a bullish candle -> long.
    SL below the day's low. Target the swing high since the low, or 2R."""
    n = len(df)
    hi, lo, cl, op, atr = (_a(df, c) for c in ("high", "low", "close", "open", "atr"))
    k = df["k"].to_numpy()
    b = cfg.get("bounce", 2.0)
    cond = np.zeros(n, bool); stop = np.full(n, np.nan); tgt = np.full(n, np.nan)
    dl = np.nan; hi_since = np.nan
    for i in range(n):
        if k[i] == 0:
            dl, hi_since = lo[i], hi[i]
            continue
        bounced = hi_since - dl >= b * atr[i]
        if (k[i] >= 6 and bounced and lo[i] <= dl + NEAR * atr[i]
                and lo[i] >= dl - NEAR * atr[i] and cl[i] > op[i]):
            cond[i], stop[i], tgt[i] = True, min(dl, lo[i]), hi_since
        if lo[i] < dl:
            dl, hi_since = lo[i], hi[i]
        else:
            hi_since = max(hi_since, hi[i])
    return _finish(df, cfg, cond, stop, stop, tgt)


# ───────────────────────────── Reversal days (Ch 9) ─────────────────────────

def odr(df, cfg):
    """1.15 Open Drive Rejection: opens BELOW PDL with a red first candle, then
    price closes above the first candle's high -> long. SL entry candle / first
    candle low. Target PDL (gap border) or CPR, else 2R."""
    red_fc = _a(df, "fc_c") < _a(df, "fc_o")
    gap_dn = _a(df, "d_open") < _a(df, "pdl")
    cond = red_fc & gap_dn & _cross_up(df, _a(df, "fc_h"))
    tgt = np.where(_a(df, "pdl") > _a(df, "close"), _a(df, "pdl"), _a(df, "cpr_bot"))
    return _finish(df, cfg, cond, _a(df, "low"), _a(df, "fc_l"), tgt)


def fc_break(df, cfg):
    """1.17 RCBO (long) / 1.16 GCBO (mirror): big RED first candle, then a green
    candle closes above its high -> long. Skipped if the close is inside the
    CPR. SL entry candle / breakout line. Target next resistance or 2R."""
    big_red = ((_a(df, "fc_h") - _a(df, "fc_l")) >= BIG * _a(df, "atr")) & (_a(df, "fc_c") < _a(df, "fc_o"))
    cl = _a(df, "close")
    inside = (cl >= _a(df, "cpr_bot")) & (cl <= _a(df, "cpr_top"))
    green = cl > _a(df, "open")
    cond = big_red & green & ~inside & _cross_up(df, _a(df, "fc_h"))
    return _finish(df, cfg, cond, _a(df, "low"), _a(df, "fc_h"),
                   _next_above(df, ["pp", "r1", "r2", "pdh"]))


def vcpr_rev(df, cfg):
    """1.18 Virgin CPR Reversal: day opened above yesterday's virgin CPR; price
    falls into it and a bullish candle closes there -> long. SL below the
    virgin CPR / entry candle. Target today's CPR or 2R."""
    top, bot = _a(df, "vcpr_top"), _a(df, "vcpr_bot")
    from_above = _a(df, "d_open") > top
    into = (_a(df, "low") <= top) & (_a(df, "close") >= bot)
    bull = _a(df, "close") > _a(df, "open")
    cond = from_above & into & bull
    return _finish(df, cfg, cond, _a(df, "low"), bot, _a(df, "cpr_bot"))


def gap_reject(df, cfg):
    """1.19 Gap Down Rejection (long) / 1.20 Gap Up Rejection (mirror): opens
    below PDL; entry=1 on a bullish FIRST candle's close, entry=2 when the
    second candle closes above the first candle's high. SL first candle low.
    Target PDL (gap border) or 2R."""
    k = df["k"].to_numpy()
    gap_dn = _a(df, "d_open") < _a(df, "pdl")
    bull_fc = _a(df, "fc_c") > _a(df, "fc_o")
    if cfg.get("entry", 1) == 1:
        cond = gap_dn & bull_fc & (k == 0)
    else:
        cond = gap_dn & bull_fc & (k == 1) & (_a(df, "close") > _a(df, "fc_h"))
    tgt = _a(df, "ema20") if cfg.get("tgt_ema") else _a(df, "pdl")   # book: "sloping 20 EMA" or PDL
    return _finish(df, cfg, cond, _a(df, "fc_l"), _a(df, "fc_l"), tgt)


def cpr_rev(df, cfg):
    """1.21 CPR Reversal: after a big crash (>= 2 ATR over 6 bars) price
    reaches the CPR from above; a bullish candle there -> long. SL entry candle
    / below CPR. Target the day's high or 2R."""
    cl, atr = _a(df, "close"), _a(df, "atr")
    crash = (np.r_[np.full(6, np.nan), cl[:-6]] - cl) >= 2.0 * atr
    crash |= (_a(df, "hi_so_far") - _a(df, "low")) >= 2.0 * atr * 1.5
    touch = (_a(df, "low") <= _a(df, "cpr_top")) & (cl >= _a(df, "cpr_bot"))
    from_above = _a(df, "d_open") > _a(df, "cpr_top")
    bull = cl > _a(df, "open")
    cond = crash & touch & from_above & bull
    return _finish(df, cfg, cond, _a(df, "low"), _a(df, "cpr_bot"), _a(df, "hi_so_far"))


def extreme_rev(df, cfg):
    """1.22 Extreme Candle Reversal: the previous candle is an extreme red bar
    (>= 2.5 ATR) whose low reached support (PDL / S1 / S2 / pivot / demand
    zone, within NEAR ATR); the current candle is bullish -> long. SL entry
    candle. Target CPR or the day's high, else 2R."""
    atr = _a(df, "atr")
    o1, c1 = np.r_[np.nan, _a(df, "open")[:-1]], np.r_[np.nan, _a(df, "close")[:-1]]
    h1, l1 = np.r_[np.nan, _a(df, "high")[:-1]], np.r_[np.nan, _a(df, "low")[:-1]]
    extreme = ((h1 - l1) >= 2.5 * atr) & (c1 < o1)
    sup = np.vstack([_a(df, c) for c in ("pdl", "s1", "s2", "pp", "dz")])
    at_sup = np.any(np.abs(sup - l1) <= NEAR * atr, axis=0)
    bull = _a(df, "close") > _a(df, "open")
    same = df["k"].to_numpy() >= 1
    cond = extreme & at_sup & bull & same
    tgt = np.where(_a(df, "cpr_bot") > _a(df, "close"), _a(df, "cpr_bot"), _a(df, "hi_so_far"))
    return _finish(df, cfg, cond, _a(df, "low"), _a(df, "low"), tgt)


def zone_rev(df, cfg):
    """1.23 Supply Zone Reversal is the MIRROR of this (long = demand zone
    reversal): price falls into the demand zone below; a bullish candle there
    -> long. SL entry candle. Target the day's high or 2R."""
    dz, atr = _a(df, "dz"), _a(df, "atr")
    into = (_a(df, "low") <= dz + NEAR * atr) & (_a(df, "close") > dz)
    bull = _a(df, "close") > _a(df, "open")
    cond = into & bull & (df["k"].to_numpy() >= 1)
    return _finish(df, cfg, cond, _a(df, "low"), dz, _a(df, "hi_so_far"))


def gapborder_rej(df, cfg):
    """1.24 Gap Border Rejection is the MIRROR of this (long = bounce off the
    upper border of an unfilled gap zone BELOW): price drops to it, a bullish
    candle there -> long. SL entry candle. Target the day's high or 2R."""
    b, atr = _a(df, "gzb_hi"), _a(df, "atr")
    into = (_a(df, "low") <= b + NEAR * atr) & (_a(df, "close") > b)
    bull = _a(df, "close") > _a(df, "open")
    cond = into & bull & (df["k"].to_numpy() >= 1)
    return _finish(df, cfg, cond, _a(df, "low"), b, _a(df, "hi_so_far"))


def fake_break(df, cfg):
    """1.25 Fake Breakout is the MIRROR of this (long = fake breakdown): price
    breaks below the day's low, then within 3 bars a bullish candle closes back
    above that broken low -> long. SL below the swing low. Target day's high or 2R."""
    n = len(df)
    lo, cl, op = _a(df, "low"), _a(df, "close"), _a(df, "open")
    lo_prev, k = _a(df, "lo_prev"), df["k"].to_numpy()
    hi = _a(df, "hi_so_far")
    cond = np.zeros(n, bool); swing = np.full(n, np.nan)
    line, start, sw = np.nan, -1, np.nan
    for i in range(n):
        if k[i] == 0:
            line, start, sw = np.nan, -1, np.nan
            continue
        if start < 0 and k[i] >= 3 and lo[i] < lo_prev[i]:
            line, start, sw = lo_prev[i], i, lo[i]
            continue
        if start >= 0:
            sw = min(sw, lo[i])
            if i - start > 3:
                start = -1
            elif cl[i] > line and cl[i] > op[i]:
                cond[i], swing[i] = True, sw
                start = -1
    return _finish(df, cfg, cond, swing, swing, hi)
