import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Intraday reference levels — everything a pivot / CPR / price-action trader
marks on the chart before (or during) the session. First used by book #1
(Vikram Prabhu, "25 Day Trading Strategies in Nifty & Bank Nifty").

Needs an intraday frame with OHLC and the `day` column from
engine/vpa.add_vpa_features (also uses its `atr`).

Every column is known at the CLOSE of its bar: day levels come from earlier
sessions only; "so far" values exclude the current bar where the strategy
needs that; first-candle / opening-range values appear only once those bars
have closed.

Works unchanged on a price-mirrored frame (vpa.mirror): PDH becomes -PDL,
TC/BC swap, R1 becomes -S1, gaps/zones above become below — which is how
the short side of every strategy is built.

Columns added
-------------
pdh pdl pdc            previous session high / low / close
pp bc tc cpr_top cpr_bot   CPR (book formula) and its top / bottom
r1 r2 r3 s1 s2 s3      standard pivots
cpr_w                  CPR width / |pp|
cpr_rank               percentile of today's cpr_w vs the previous 60 sessions
vcpr_top vcpr_bot      YESTERDAY's CPR if yesterday never traded into it
                       (a "virgin CPR"), else NaN
gz_lo gz_hi            nearest unfilled daily gap zone ABOVE today's open
                       (remaining unfilled part), NaN if none in 60 sessions
gzb_lo gzb_hi          nearest unfilled daily gap zone BELOW today's open
sz                     nearest supply zone ABOVE today's open (15-min swing
                       high followed by a crash, last 3 sessions)
dz                     nearest demand zone BELOW today's open
ema8 ema20             continuous EMAs of close
d_ema20                daily 20 EMA as of yesterday's close
or3_h or3_l            15-minute opening range (first 3 bars), from bar 2
k                      bar number within the session (0 = first candle)
d_open                 session open
fc_o fc_h fc_l fc_c    first candle OHLC (NaN on the first bar itself? no —
                       known at its close, so present from bar 0)
or_h or_l              opening-range high / low over the first `or_bars`
                       bars, present from bar or_bars-1
hi_so_far lo_so_far    session high / low INCLUDING the current bar
hi_prev lo_prev        session high / low BEFORE the current bar
"""

import numpy as np
import pandas as pd

DEFAULTS = {"or_bars": 6, "rank_days": 60, "gap_days": 60, "zone_days": 3,
            "zone_crash_atr": 2.5, "zone_fwd_bars": 4}


def _daily(df):
    g = df.groupby("day")
    return pd.DataFrame({"o": g["open"].first(), "h": g["high"].max(),
                         "l": g["low"].min(), "c": g["close"].last()})


def _gap_zones(d: pd.DataFrame, max_days: int):
    """Per day: nearest still-unfilled daily gap zone above / below the open."""
    o, h, l = d["o"].to_numpy(), d["h"].to_numpy(), d["l"].to_numpy()
    n = len(d)
    up_lo = np.full(n, np.nan); up_hi = np.full(n, np.nan)
    dn_lo = np.full(n, np.nan); dn_hi = np.full(n, np.nan)
    for t in range(2, n):
        best_above, best_below = None, None
        for g in range(max(1, t - max_days), t):
            hi_after = h[g:t].max()           # highest high from the gap day to yesterday
            lo_after = l[g:t].min()
            if h[g] < l[g - 1]:               # gap DOWN on day g: zone [h[g], l[g-1]]
                lo_z = max(h[g], hi_after)    # filled from below as price rose into it
                if lo_z < l[g - 1] and lo_z >= o[t]:
                    if best_above is None or lo_z < best_above[0]:
                        best_above = (lo_z, l[g - 1])
            if l[g] > h[g - 1]:               # gap UP on day g: zone [h[g-1], l[g]]
                hi_z = min(l[g], lo_after)
                if hi_z > h[g - 1] and hi_z <= o[t]:
                    if best_below is None or hi_z > best_below[1]:
                        best_below = (h[g - 1], hi_z)
        if best_above:
            up_lo[t], up_hi[t] = best_above
        if best_below:
            dn_lo[t], dn_hi[t] = best_below
    return up_lo, up_hi, dn_lo, dn_hi


def _zones(df: pd.DataFrame, d: pd.DataFrame, c: dict):
    """Supply above / demand below each day's open, from 15-min swings of the
    previous `zone_days` sessions that were followed by a crash / rally."""
    m15 = (df[["open", "high", "low", "close"]]
           .resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
           .dropna())
    m15["day"] = m15.index.normalize()
    tr = pd.concat([m15.high - m15.low, (m15.high - m15.close.shift()).abs(),
                    (m15.low - m15.close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=5).mean().to_numpy()
    H, L, D = m15.high.to_numpy(), m15.low.to_numpy(), m15.day.to_numpy()
    fwd, k = c["zone_fwd_bars"], c["zone_crash_atr"]
    sup, dem = {}, {}
    for j in range(2, len(m15) - fwd):
        if not atr[j] == atr[j] or D[j + fwd] != D[j]:
            continue                  # the crash must complete inside that same past session
        if H[j] >= H[j - 2:j + 3].max() and L[j + 1:j + 1 + fwd].min() <= H[j] - k * atr[j]:
            sup.setdefault(D[j], []).append(H[j])
        if L[j] <= L[j - 2:j + 3].min() and H[j + 1:j + 1 + fwd].max() >= L[j] + k * atr[j]:
            dem.setdefault(D[j], []).append(L[j])
    days = d.index.to_numpy(); o = d["o"].to_numpy()
    sz = np.full(len(d), np.nan); dz = np.full(len(d), np.nan)
    for t in range(len(d)):
        prev = days[max(0, t - c["zone_days"]):t]
        s = [x for p in prev for x in sup.get(p, []) if x > o[t]]
        z = [x for p in prev for x in dem.get(p, []) if x < o[t]]
        if s: sz[t] = min(s)
        if z: dz[t] = max(z)
    return sz, dz


def add_levels(df: pd.DataFrame, cfg: dict = None) -> pd.DataFrame:
    c = {**DEFAULTS, **(cfg or {})}
    out = df.copy()
    d = _daily(out)
    ph, pl, pc = d["h"].shift(1), d["l"].shift(1), d["c"].shift(1)
    pp = (ph + pl + pc) / 3
    bc = (ph + pl) / 2
    tc = 2 * pp - bc
    lv = pd.DataFrame({
        "pdh": ph, "pdl": pl, "pdc": pc, "pp": pp, "bc": bc, "tc": tc,
        "cpr_top": np.maximum(tc, bc), "cpr_bot": np.minimum(tc, bc),
        "r1": 2 * pp - pl, "s1": 2 * pp - ph, "r2": pp + (ph - pl), "s2": pp - (ph - pl),
        "r3": ph + 2 * (pp - pl), "s3": pl - 2 * (ph - pp)}, index=d.index)
    lv["cpr_w"] = (lv.cpr_top - lv.cpr_bot) / lv.pp.abs()
    lv["cpr_rank"] = lv["cpr_w"].rolling(c["rank_days"] + 1, min_periods=20).apply(
        lambda x: (x[:-1] < x[-1]).mean(), raw=True)
    # virgin CPR: yesterday's CPR (built from day-2) that yesterday never touched
    y_top, y_bot = lv["cpr_top"].shift(1), lv["cpr_bot"].shift(1)
    touched = (pl <= y_top) & (ph >= y_bot)
    lv["vcpr_top"] = y_top.where(~touched)
    lv["vcpr_bot"] = y_bot.where(~touched)
    up_lo, up_hi, dn_lo, dn_hi = _gap_zones(d, c["gap_days"])
    lv["gz_lo"], lv["gz_hi"], lv["gzb_lo"], lv["gzb_hi"] = up_lo, up_hi, dn_lo, dn_hi
    lv["sz"], lv["dz"] = _zones(out, d, c)
    lv["d_open"] = d["o"]
    # daily 20 EMA of closes, as of YESTERDAY's close (known before the open)
    lv["d_ema20"] = d["c"].ewm(span=20, adjust=False).mean().shift(1)
    out = out.join(lv, on="day")

    g = out.groupby("day")
    out["k"] = g.cumcount()
    out["ema8"] = out["close"].ewm(span=8, adjust=False).mean()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    for col, src in (("fc_o", "open"), ("fc_h", "high"), ("fc_l", "low"), ("fc_c", "close")):
        out[col] = g[src].transform("first")
    n = c["or_bars"]
    in_or = out["k"] < n
    or_h = out["high"].where(in_or).groupby(out["day"]).transform("max")
    or_l = out["low"].where(in_or).groupby(out["day"]).transform("min")
    ready = out["k"] >= n - 1
    out["or_h"], out["or_l"] = or_h.where(ready), or_l.where(ready)
    # 15-minute opening range (first 3 bars) as a variant
    in3 = out["k"] < 3
    o3h = out["high"].where(in3).groupby(out["day"]).transform("max")
    o3l = out["low"].where(in3).groupby(out["day"]).transform("min")
    out["or3_h"], out["or3_l"] = o3h.where(out["k"] >= 2), o3l.where(out["k"] >= 2)
    out["hi_so_far"] = g["high"].cummax()
    out["lo_so_far"] = g["low"].cummin()
    out["hi_prev"] = out.groupby("day")["hi_so_far"].shift(1)
    out["lo_prev"] = out.groupby("day")["lo_so_far"].shift(1)
    return out
