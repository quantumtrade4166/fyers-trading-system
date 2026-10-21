import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Volume Price Analysis (VPA) feature library
===========================================
Objective versions of the candle/volume ideas in Anna Coulling's
"A Complete Guide to Volume Price Analysis" (library book #11), written to be
reused by any book that needs volume, spread, wick or pivot logic.

Every column is known at the CLOSE of its bar — nothing looks ahead.
Averages compare a bar with PREVIOUS bars only (shift(1) before rolling).

Volume is measured as TURNOVER (price x shares), so a stock split does not
fake a volume spike. Intraday averages are taken per TIME-OF-DAY SLOT
(this 09:15 bar vs the last 20 days' 09:15 bars) because the open and close
of an NSE session always carry far more volume than midday.

Columns added by add_vpa_features():
    day            session date
    atr            ATR(14), simple mean of true range (previous close
                   only from the same session when intraday)
    rv             relative volume (turnover / its prior average)
    body_rel       candle body size vs its prior average
    range_rel      high-low range vs its prior average
    up, down       candle colour
    upper_wick,
    lower_wick     wick length as a fraction of the bar range
    body_frac      body as a fraction of the bar range
    close_pos      where the close sits in the range (0 = low, 1 = high)
    shooting_star, hammer, ll_doji   candle shapes (shape only, no volume)
    ph, pl         a pivot high / low was CONFIRMED on this bar
    ph_level,
    pl_level       price of that pivot (the middle bar's high / low)
    mom            N-bar move in ATR units (within the session if intraday)
"""

import numpy as np
import pandas as pd

DEFAULTS = {
    "avg_n":        20,     # bars (or days, per slot) in every "average"
    "min_periods":  10,
    "atr_n":        14,
    "mom_n":        10,     # bars for the trend/momentum context
    # candle-shape thresholds (fractions of the bar range)
    "star_wick":    0.50,   # long wick must be >= half the range
    "star_body_x":  2.0,    # long wick >= 2x body
    "star_other":   0.25,   # opposite wick <= quarter of the range
    "doji_body":    0.10,
    "doji_wick":    0.35,
}


def _prior_mean(s: pd.Series, n: int, mp: int, groups=None) -> pd.Series:
    """Mean of the previous n values (current value excluded)."""
    if groups is None:
        return s.shift(1).rolling(n, min_periods=mp).mean()
    return s.groupby(groups).transform(
        lambda x: x.shift(1).rolling(n, min_periods=mp).mean())


def add_vpa_features(df: pd.DataFrame, intraday: bool, cfg: dict = None) -> pd.DataFrame:
    c = {**DEFAULTS, **(cfg or {})}
    out = df.copy()
    o, h, l, cl = out["open"], out["high"], out["low"], out["close"]

    out["day"] = out.index.normalize()
    # Does this market have SESSIONS (NSE: yes; crypto 24/7 and daily bars: no)?
    # Strategies read this instead of guessing from bars-per-day.
    out.attrs["sessions"] = bool(intraday)
    slot = out.index.time if intraday else None
    n, mp = c["avg_n"], c["min_periods"]

    # ── ATR (session-aware: the first bar of a day ignores last night's close)
    prev_close = cl.shift(1)
    if intraday:
        prev_close = prev_close.where(out["day"] == out["day"].shift(1))
    tr = pd.concat([h - l, (h - prev_close).abs(), (l - prev_close).abs()],
                   axis=1).max(axis=1)
    out["atr"] = tr.rolling(c["atr_n"], min_periods=c["atr_n"]).mean()

    # ── Relative volume on turnover
    px = cl.abs()   # abs(): the short side runs on a price-mirrored copy (see mirror())
    turnover = out["volume"].astype(float) * px
    out["rv"] = turnover / _prior_mean(turnover, n, mp, slot)

    # ── Spread, as % of price so splits don't matter
    body_pct = (cl - o).abs() / px
    range_pct = (h - l) / px
    out["body_rel"] = body_pct / _prior_mean(body_pct, n, mp, slot)
    out["range_rel"] = range_pct / _prior_mean(range_pct, n, mp, slot)

    # ── Candle anatomy
    rng = (h - l).replace(0, np.nan)
    body = (cl - o).abs()
    out["up"] = cl > o
    out["down"] = cl < o
    out["upper_wick"] = (h - np.maximum(o, cl)) / rng
    out["lower_wick"] = (np.minimum(o, cl) - l) / rng
    out["body_frac"] = body / rng
    out["close_pos"] = (cl - l) / rng

    uw = h - np.maximum(o, cl)
    lw = np.minimum(o, cl) - l
    out["shooting_star"] = ((out["upper_wick"] >= c["star_wick"])
                            & (uw >= c["star_body_x"] * body)
                            & (out["lower_wick"] <= c["star_other"]))
    out["hammer"] = ((out["lower_wick"] >= c["star_wick"])
                     & (lw >= c["star_body_x"] * body)
                     & (out["upper_wick"] <= c["star_other"]))
    out["ll_doji"] = ((out["body_frac"] <= c["doji_body"])
                      & (out["upper_wick"] >= c["doji_wick"])
                      & (out["lower_wick"] >= c["doji_wick"]))

    # ── Isolated pivots (Coulling Fig 7.13): the middle of 3 bars has a
    #    higher high AND higher low than both neighbours (mirror for a low).
    #    Confirmed only when the 3rd bar closes -> flagged on that bar.
    h1, h2, l1, l2 = h.shift(1), h.shift(2), l.shift(1), l.shift(2)
    ph = (h1 > h2) & (h1 > h) & (l1 > l2) & (l1 > l)
    pl = (l1 < l2) & (l1 < l) & (h1 < h2) & (h1 < h)
    if intraday:  # all three bars must be in the same session
        same = (out["day"] == out["day"].shift(2))
        ph &= same
        pl &= same
    out["ph"] = ph.fillna(False)
    out["pl"] = pl.fillna(False)
    out["ph_level"] = h1.where(out["ph"])
    out["pl_level"] = l1.where(out["pl"])

    # ── Momentum / trend context in ATR units
    move = cl - cl.shift(c["mom_n"])
    if intraday:
        move = move.where(out["day"] == out["day"].shift(c["mom_n"]))
    out["mom"] = move / out["atr"]

    return out


def vol_class(rv: pd.Series) -> pd.Series:
    """Coulling's low / average / above / high / ultra buckets (for reports)."""
    bins = [-np.inf, 0.75, 1.25, 1.75, 2.5, np.inf]
    return pd.cut(rv, bins, labels=["low", "average", "above", "high", "ultra"])


def last_pivots(df: pd.DataFrame) -> pd.DataFrame:
    """
    Forward-filled most recent confirmed pivot levels and the bar number each
    was confirmed on — the raw material for congestion zones and dynamic
    trend lines. Also the PREVIOUS pivot of each kind, for higher-high /
    higher-low checks.
    """
    i = np.arange(len(df))
    out = pd.DataFrame(index=df.index)
    for k in ("ph", "pl"):
        lvl = df[f"{k}_level"]
        out[f"{k}_last"] = lvl.ffill()
        out[f"{k}_bar"] = pd.Series(np.where(df[k], i, np.nan), index=df.index).ffill()
        prev = lvl.dropna().shift(1).reindex(df.index).ffill()
        out[f"{k}_prev"] = prev
    return out


def mirror(df: pd.DataFrame) -> pd.DataFrame:
    """
    Price-mirrored copy: a falling market becomes a rising one. Running a
    LONG-only strategy on this copy gives its exact SHORT twin, so both sides
    of every strategy are guaranteed to use the same rules.
    """
    m = df.copy()
    m["open"], m["close"] = -df["open"], -df["close"]
    m["high"], m["low"] = -df["low"], -df["high"]
    return m
