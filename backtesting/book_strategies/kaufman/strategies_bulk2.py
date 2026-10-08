import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Kaufman remaining batches — patterns, candlesticks, regression, multi-TF,
Fibonacci, seasonality, cycle
=============================================================================
Covers the categories still untested after the first 41 strategies:

  S001-S010  one-day bar patterns & gaps
  S011-S018  candlestick patterns
  S030,S041  Wilder swing index, projected MA crossover
  S042-S044  regression family (siblings of S019, our best validated strategy)
  S049,S053,S055,S061,S063,S064  remaining oscillators
  S075,S076,S139  Fibonacci
  S094-S096  seasonality
  S099-S102  cycle
  S113-S117  multiple time frames

Deliberate simplifications (all flagged so results are not over-read):
  • S009 island reversal needs two consecutive gaps — near-zero signals on
    24x5 instruments. Kept for completeness; expect no trades.
  • S045 ARIMA, S093 MAMA, S140-S142 ML: excluded (compute / dependencies).
  • S099 uses a fixed dominant-cycle estimate rather than rolling FFT — a
    rolling FFT over millions of bars is not worth the compute for a
    strategy family that rarely survives validation.
  • S123-S125 price-distribution strategies use rolling quantiles rather than
    full KDE / TPO reconstruction.
  • Volume-dependent filters are dropped everywhere (no volume in this data).
"""

import numpy as np
import pandas as pd

from backtesting.indicators import add_ema, add_rsi
from backtesting.indicators_ext import (
    add_atr, add_sma, add_roc, add_momentum, add_macd, add_linreg_channel,
    add_swings, add_cci, add_williams_r, add_cmo,
)
from backtesting.book_strategies.kaufman.harness import blank_signals


def _atr(df, p=14):
    if f"atr_{p}" not in df.columns:
        df = add_atr(df, period=p)
    return df


def _ent(long_s, short_s):
    e = np.zeros(len(long_s), dtype=int)
    ls = long_s.fillna(False).to_numpy(); ss = short_s.fillna(False).to_numpy()
    e[ls] = 1; e[ss & ~ls] = -1
    return e


def _xup(a, b): return (a.shift(1) <= b.shift(1)) & (a > b)
def _xdn(a, b): return (a.shift(1) >= b.shift(1)) & (a < b)


def _lvl(df, v): return pd.Series(float(v), index=df.index)


def _stops(sig, df, mult, p=14):
    atr = df[f"atr_{p}"]
    sig["stop_long"]  = df["close"] - mult * atr
    sig["stop_short"] = df["close"] + mult * atr
    return sig


# ════════════ S001-S010 — bar patterns & gaps ════════════

def s001_002_key_reversal(df, cfg):
    """S001/S002 combined: new extreme then close against it."""
    lb = cfg.get("lookback", 20); m = cfg.get("stop_atr_mult", 2.0)
    df = _atr(df)
    hh = df["high"].rolling(lb).max().shift(1)
    ll = df["low"].rolling(lb).min().shift(1)
    c, pc = df["close"], df["close"].shift(1)
    sig = blank_signals(df)
    sig["entry"] = _ent((df["low"] < ll) & (c > pc), (df["high"] > hh) & (c < pc))
    sig["stop_long"]  = df["low"]
    sig["stop_short"] = df["high"]
    return sig


def s003_outside_day(df, cfg):
    m = cfg.get("stop_atr_mult", 2.0); df = _atr(df)
    out = (df["high"] > df["high"].shift(1)) & (df["low"] < df["low"].shift(1))
    c, pc = df["close"], df["close"].shift(1)
    sig = blank_signals(df)
    sig["entry"] = _ent(out & (c > pc), out & (c < pc))
    sig["stop_long"] = df["low"]; sig["stop_short"] = df["high"]
    return sig


def s004_inside_day(df, cfg):
    m = cfg.get("entry_mult", 1.0); df = _atr(df)
    atr = df["atr_14"]
    ins = (df["high"] < df["high"].shift(1)) & (df["low"] > df["low"].shift(1))
    trig_up = df["high"].shift(1) + m * atr
    trig_dn = df["low"].shift(1) - m * atr
    sig = blank_signals(df)
    sig["entry"] = _ent(ins.shift(1).fillna(False) & (df["high"] > trig_up),
                        ins.shift(1).fillna(False) & (df["low"] < trig_dn))
    sig["stop_long"] = df["low"]; sig["stop_short"] = df["high"]
    return sig


def s005_outside_close(df, cfg):
    df = _atr(df)
    out = (df["high"] > df["high"].shift(1)) & (df["low"] < df["low"].shift(1))
    sig = blank_signals(df)
    sig["entry"] = _ent(out & (df["close"] > df["high"].shift(1)),
                        out & (df["close"] < df["low"].shift(1)))
    mid = (df["high"] + df["low"]) / 2
    sig["exit_long"] = df["close"] < mid
    sig["exit_short"] = df["close"] > mid
    sig["stop_long"] = df["low"]; sig["stop_short"] = df["high"]
    return sig


def s006_gap_fade(df, cfg):
    """⚠️ 24x5 instruments gap rarely — expect very few signals."""
    thr = cfg.get("gap_threshold", 0.003); m = cfg.get("stop_atr_mult", 1.0)
    df = _atr(df)
    pc = df["close"].shift(1)
    gap = (df["open"] - pc) / pc
    sig = blank_signals(df)
    sig["entry"] = _ent(gap < -thr, gap > thr)     # fade the gap
    sig["exit_long"] = df["close"] >= pc
    sig["exit_short"] = df["close"] <= pc
    return _stops(sig, df, m)


def s007_gap_continuation(df, cfg):
    """Volume confirmation dropped — no volume in this data."""
    thr = cfg.get("gap_threshold", 0.005); m = cfg.get("stop_atr_mult", 1.5)
    df = _atr(df)
    pc = df["close"].shift(1)
    gap = (df["open"] - pc) / pc
    sig = blank_signals(df)
    sig["entry"] = _ent(gap > thr, gap < -thr)     # ride the gap
    atr = df["atr_14"]; tn = cfg.get("trail_bars", 20)
    sig["stop_long"]  = df["high"].rolling(tn, min_periods=1).max() - m * atr
    sig["stop_short"] = df["low"].rolling(tn, min_periods=1).min() + m * atr
    return sig


def s009_island_reversal(df, cfg):
    """⚠️ Requires TWO consecutive gaps — expect ~zero signals on 24x5 data."""
    df = _atr(df)
    gap_up_prev = df["open"].shift(1) > df["high"].shift(2)
    gap_dn_now  = df["open"] < df["low"].shift(1)
    gap_dn_prev = df["open"].shift(1) < df["low"].shift(2)
    gap_up_now  = df["open"] > df["high"].shift(1)
    sig = blank_signals(df)
    sig["entry"] = _ent(gap_dn_prev & gap_up_now, gap_up_prev & gap_dn_now)
    return _stops(sig, df, cfg.get("stop_atr_mult", 1.0))


def s010_pivot_points(df, cfg):
    """Floor pivots from the prior bar. Volume-confirmed breakout leg dropped."""
    m = cfg.get("stop_atr_mult", 1.0); df = _atr(df)
    h, l, c = df["high"].shift(1), df["low"].shift(1), df["close"].shift(1)
    pp = (h + l + c) / 3
    r1 = 2 * pp - l; s1 = 2 * pp - h
    px = df["close"]
    sig = blank_signals(df)
    sig["entry"] = _ent(px < s1, px > r1)          # bounce off the outer levels
    sig["exit_long"] = px >= pp
    sig["exit_short"] = px <= pp
    return _stops(sig, df, m)


# ════════════ S011-S018 — candlesticks ════════════

def _body_parts(df):
    body = (df["close"] - df["open"]).abs()
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    upper = df["high"] - df[["open", "close"]].max(axis=1)
    lower = df[["open", "close"]].min(axis=1) - df["low"]
    return body, rng, upper, lower


def s011_hammer_engulf(df, cfg):
    tp = cfg.get("trend_ma", 20); br = cfg.get("body_ratio", 0.3)
    sr = cfg.get("shadow_ratio", 2.0); m = cfg.get("stop_atr_mult", 0.5)
    df = add_sma(df, period=tp); df = _atr(df)
    body, rng, upper, lower = _body_parts(df)
    down = df["close"] < df[f"sma_{tp}"]
    hammer = (body < br * rng) & (lower > sr * body) & (upper < body) & down
    engulf = (df["close"] > df["open"]) & (df["close"].shift(1) < df["open"].shift(1)) & \
             (df["open"] < df["close"].shift(1)) & (df["close"] > df["open"].shift(1))
    sig = blank_signals(df)
    sig["entry"] = _ent(hammer | engulf, pd.Series(False, index=df.index))
    sig["exit_long"] = df["close"] >= df[f"sma_{tp}"]
    sig["stop_long"] = df["low"] - m * df["atr_14"]
    return sig


def s012_star_engulf_short(df, cfg):
    tp = cfg.get("trend_ma", 20); br = cfg.get("body_ratio", 0.3)
    sr = cfg.get("shadow_ratio", 2.0); m = cfg.get("stop_atr_mult", 0.5)
    df = add_sma(df, period=tp); df = _atr(df)
    body, rng, upper, lower = _body_parts(df)
    up = df["close"] > df[f"sma_{tp}"]
    star = (body < br * rng) & (upper > sr * body) & (lower < body) & up
    engulf = (df["close"] < df["open"]) & (df["close"].shift(1) > df["open"].shift(1)) & \
             (df["open"] > df["close"].shift(1)) & (df["close"] < df["open"].shift(1))
    sig = blank_signals(df)
    sig["entry"] = _ent(pd.Series(False, index=df.index), star | engulf)
    sig["exit_short"] = df["close"] <= df[f"sma_{tp}"]
    sig["stop_short"] = df["high"] + m * df["atr_14"]
    return sig


def s013_doji(df, cfg):
    dr = cfg.get("doji_ratio", 0.05); m = cfg.get("stop_atr_mult", 0.5)
    df = _atr(df)
    body, rng, _, _ = _body_parts(df)
    doji = (rng > 0) & (body / rng < dr)
    at_low  = (df["low"] < df["low"].shift(1)) & (df["low"] < df["low"].shift(2))
    at_high = (df["high"] > df["high"].shift(1)) & (df["high"] > df["high"].shift(2))
    sig = blank_signals(df)
    sig["entry"] = _ent(doji & at_low, doji & at_high)
    sig["stop_long"] = df["low"] - m * df["atr_14"]
    sig["stop_short"] = df["high"] + m * df["atr_14"]
    return sig


def s014_015_star(df, cfg):
    """Morning / evening star (3-bar reversal)."""
    df = _atr(df); atr = df["atr_14"]
    o, c = df["open"], df["close"]
    big_dn = (o.shift(2) - c.shift(2)) > 0.6 * atr.shift(2)
    big_up = (c.shift(2) - o.shift(2)) > 0.6 * atr.shift(2)
    small  = (c.shift(1) - o.shift(1)).abs() < 0.3 * atr.shift(1)
    mid2   = (o.shift(2) + c.shift(2)) / 2
    morning = big_dn & small & (c > mid2) & (c > o)
    evening = big_up & small & (c < mid2) & (c < o)
    sig = blank_signals(df)
    sig["entry"] = _ent(morning, evening)
    sig["stop_long"]  = df["low"].shift(1)
    sig["stop_short"] = df["high"].shift(1)
    return sig


def s016_017_soldiers_crows(df, cfg):
    bmin = cfg.get("body_min_pct", 0.6); m = cfg.get("stop_atr_mult", 2.0)
    df = _atr(df)
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    bull = df["close"] > df["open"]
    bear = df["close"] < df["open"]
    strong = ((df["close"] - df["open"]).abs() / rng) > bmin
    three_up = bull & bull.shift(1) & bull.shift(2) & strong & strong.shift(1) & strong.shift(2) \
               & (df["close"] > df["close"].shift(1)) & (df["close"].shift(1) > df["close"].shift(2))
    three_dn = bear & bear.shift(1) & bear.shift(2) & strong & strong.shift(1) & strong.shift(2) \
               & (df["close"] < df["close"].shift(1)) & (df["close"].shift(1) < df["close"].shift(2))
    sig = blank_signals(df)
    sig["entry"] = _ent(three_up, three_dn)
    tn = cfg.get("trail_bars", 20); atr = df["atr_14"]
    sig["stop_long"]  = df["high"].rolling(tn, min_periods=1).max() - m * atr
    sig["stop_short"] = df["low"].rolling(tn, min_periods=1).min() + m * atr
    return sig


def s018_harami(df, cfg):
    m = cfg.get("stop_atr_mult", 1.0); df = _atr(df)
    o, c = df["open"], df["close"]
    bull_h = (c.shift(1) < o.shift(1)) & (o > c.shift(1)) & (c < o.shift(1)) & (c > o)
    bear_h = (c.shift(1) > o.shift(1)) & (o < c.shift(1)) & (c > o.shift(1)) & (c < o)
    sig = blank_signals(df)
    sig["entry"] = _ent(bull_h, bear_h)
    sig["stop_long"] = df["low"].shift(1); sig["stop_short"] = df["high"].shift(1)
    return sig


# ════════════ S030, S041 ════════════

def s030_swing_index(df, cfg):
    """Wilder's Accumulated Swing Index — trade ASI swing breakouts."""
    m = cfg.get("stop_atr_mult", 2.0); lb = cfg.get("lookback", 20)
    df = _atr(df)
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    pc, po = c.shift(1), o.shift(1)
    k = pd.concat([(h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    r = tr.replace(0, np.nan)
    si = 50 * ((c - pc) + 0.5 * (c - o) + 0.25 * (pc - po)) / r * (k / r.clip(lower=1e-9))
    asi = si.fillna(0).cumsum()
    hi = asi.rolling(lb).max().shift(1); lo = asi.rolling(lb).min().shift(1)
    sig = blank_signals(df)
    sig["entry"] = _ent(asi > hi, asi < lo)
    return _stops(sig, df, m)


def s041_projected_ma(df, cfg):
    """Anticipate an MA crossover from the gap's rate of closure."""
    f, s = cfg.get("fast", 10), cfg.get("slow", 30)
    bars = cfg.get("bars_to_cross", 3); m = cfg.get("stop_atr_mult", 2.0)
    df = add_ema(df, period=f); df = add_ema(df, period=s); df = _atr(df)
    gap = df[f"ema_{f}"] - df[f"ema_{s}"]
    slope = gap.diff()
    eta = -gap / slope.replace(0, np.nan)
    sig = blank_signals(df)
    sig["entry"] = _ent((gap < 0) & (eta > 0) & (eta < bars),
                        (gap > 0) & (eta > 0) & (eta < bars))
    sig["exit_long"]  = gap > 0
    sig["exit_short"] = gap < 0
    return _stops(sig, df, m)


# ════════════ S042-S044 — regression family ════════════

def s042_reg_slope(df, cfg):
    w = cfg.get("reg_period", 20); sm = cfg.get("smooth", 5)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_linreg_channel(df, window=w); df = _atr(df)
    slope = df[f"lr_slope_{w}"].ewm(span=sm, adjust=False).mean()
    z = _lvl(df, 0)
    sig = blank_signals(df)
    sig["entry"] = _ent(_xup(slope, z), _xdn(slope, z))
    return _stops(sig, df, m)


def s043_forecast_osc(df, cfg):
    w = cfg.get("reg_period", 14); thr = cfg.get("threshold", 2.0)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_linreg_channel(df, window=w); df = _atr(df)
    fosc = 100 * (df["close"] - df[f"lr_mid_{w}"]) / df["close"]
    sig = blank_signals(df)
    sig["entry"] = _ent(fosc < -thr, fosc > thr)      # fade the forecast error
    z = _lvl(df, 0)
    sig["exit_long"] = fosc >= 0
    sig["exit_short"] = fosc <= 0
    return _stops(sig, df, m)


def s044_reg_band_mr(df, cfg):
    w = cfg.get("reg_period", 30); wm = cfg.get("confidence", 2.0)
    m = cfg.get("stop_atr_mult", 1.0)
    df = add_linreg_channel(df, window=w, width_mult=wm); df = _atr(df)
    c = df["close"]; up, lo, mid = df[f"lr_upper_{w}"], df[f"lr_lower_{w}"], df[f"lr_mid_{w}"]
    slope = df[f"lr_slope_{w}"]
    sig = blank_signals(df)
    sig["entry"] = _ent((c < lo) & (slope > 0), (c > up) & (slope < 0))
    sig["exit_long"] = c >= mid; sig["exit_short"] = c <= mid
    sig["stop_long"] = lo - m * df["atr_14"]
    sig["stop_short"] = up + m * df["atr_14"]
    return sig


# ════════════ remaining oscillators ════════════

def s049_mom_oscillator(df, cfg):
    p = cfg.get("mom_period", 12); ob = cfg.get("ob", 3.0); os_ = cfg.get("os", -3.0)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_roc(df, period=p); df = _atr(df)
    roc = df[f"roc_{p}"]
    sig = blank_signals(df)
    sig["entry"] = _ent(roc < os_, roc > ob)
    z = _lvl(df, 0)
    sig["exit_long"] = roc >= 0; sig["exit_short"] = roc <= 0
    return _stops(sig, df, m)


def s053_macd_divergence(df, cfg):
    lb = cfg.get("divergence_bars", 10); m = cfg.get("stop_atr_mult", 2.0)
    df = add_macd(df); df = _atr(df)
    hist = df["macd_hist"]
    ll_p = df["low"].rolling(lb).min(); ll_h = hist.rolling(lb).min()
    hh_p = df["high"].rolling(lb).max(); hh_h = hist.rolling(lb).max()
    bull = (df["low"] <= ll_p) & (hist > ll_h)
    bear = (df["high"] >= hh_p) & (hist < hh_h)
    sig = blank_signals(df)
    sig["entry"] = _ent(bull, bear)
    return _stops(sig, df, m)


def s055_stoch_rsi(df, cfg):
    rp = cfg.get("rsi_period", 14); sp = cfg.get("stoch_period", 14)
    m = cfg.get("stop_atr_mult", 1.5)
    df = add_rsi(df, period=rp); df = _atr(df)
    r = df[f"rsi_{rp}"]
    lo = r.rolling(sp).min(); hi = r.rolling(sp).max()
    k = ((r - lo) / (hi - lo).replace(0, np.nan) * 100).rolling(3).mean()
    d = k.rolling(3).mean()
    sig = blank_signals(df)
    sig["entry"] = _ent((k < 20) & _xup(k, d), (k > 80) & _xdn(k, d))
    sig["exit_long"] = k > 80; sig["exit_short"] = k < 20
    return _stops(sig, df, m)


def s061_double_smoothed_mom(df, cfg):
    s1, s2 = cfg.get("smooth1", 5), cfg.get("smooth2", 3)
    m = cfg.get("stop_atr_mult", 2.0)
    df = _atr(df)
    mom = df["close"].diff(cfg.get("mom_period", 1))
    ds = mom.ewm(span=s1, adjust=False).mean().ewm(span=s2, adjust=False).mean()
    sgn = ds.ewm(span=s2, adjust=False).mean()
    sig = blank_signals(df)
    sig["entry"] = _ent(_xup(ds, sgn), _xdn(ds, sgn))
    return _stops(sig, df, m)


def s063_divergence_index(df, cfg):
    p = cfg.get("mom_period", 14); lb = cfg.get("lookback", 20)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_momentum(df, period=p); df = _atr(df)
    mom = df[f"mom_{p}"]
    new_hi = df["high"] >= df["high"].rolling(lb).max().shift(1)
    new_lo = df["low"] <= df["low"].rolling(lb).min().shift(1)
    mom_no_hi = mom < mom.rolling(lb).max().shift(1)
    mom_no_lo = mom > mom.rolling(lb).min().shift(1)
    z = _lvl(df, 0)
    sig = blank_signals(df)
    sig["entry"] = _ent(new_lo & mom_no_lo & _xup(mom, z),
                        new_hi & mom_no_hi & _xdn(mom, z))
    return _stops(sig, df, m)


def s064_rsi_of_macd(df, cfg):
    f, s = cfg.get("fast_ma", 10), cfg.get("slow_ma", 40)
    rp = cfg.get("rsi_period", 5); m = cfg.get("stop_atr_mult", 2.0)
    df = add_ema(df, period=f); df = add_ema(df, period=s); df = _atr(df)
    spread = df[f"ema_{f}"] - df[f"ema_{s}"]
    d = spread.diff()
    up = d.clip(lower=0).ewm(alpha=1/rp, adjust=False).mean()
    dn = (-d).clip(lower=0).ewm(alpha=1/rp, adjust=False).mean()
    rsi_sp = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    sig = blank_signals(df)
    sig["entry"] = _ent(rsi_sp < cfg.get("os", 30), rsi_sp > cfg.get("ob", 70))
    sig["exit_long"] = rsi_sp >= 50; sig["exit_short"] = rsi_sp <= 50
    return _stops(sig, df, m)


# ════════════ S075/S076/S139 — Fibonacci ════════════

def s075_fib_retracement(df, cfg):
    lb = cfg.get("lookback", 50); tp = cfg.get("trend_ma", 50)
    m = cfg.get("stop_atr_mult", 0.5); rp = cfg.get("rsi_period", 14)
    df = add_sma(df, period=tp); df = add_rsi(df, period=rp); df = _atr(df)
    hi = df["high"].rolling(lb).max(); lo = df["low"].rolling(lb).min()
    rng = hi - lo
    f382 = hi - 0.382 * rng; f618 = hi - 0.618 * rng
    c, atr, sma, rsi = df["close"], df["atr_14"], df[f"sma_{tp}"], df[f"rsi_{rp}"]
    near = ((c - f382).abs() < 0.5 * atr) | ((c - f618).abs() < 0.5 * atr)
    sig = blank_signals(df)
    sig["entry"] = _ent(near & (c > sma) & (rsi < 45), near & (c < sma) & (rsi > 55))
    sig["exit_long"] = c >= hi.shift(1); sig["exit_short"] = c <= lo.shift(1)
    sig["stop_long"] = df["low"] - m * atr; sig["stop_short"] = df["high"] + m * atr
    return sig


def s139_fib_rsi(df, cfg):
    """S139: tighter RSI confirmation at the fib level."""
    c2 = dict(cfg); c2["rsi_period"] = cfg.get("rsi_period", 14)
    sig = s075_fib_retracement(df, c2)
    return sig


# ════════════ S094-S096 — seasonality ════════════

def s094_day_of_week(df, cfg):
    """Long on historically strong weekdays, short on weak ones (in-sample stats)."""
    m = cfg.get("stop_atr_mult", 2.0); thr = cfg.get("min_edge", 0.0)
    df = _atr(df)
    r = df["close"].pct_change()
    dow = pd.Series(df.index.dayofweek, index=df.index)
    # expanding mean per weekday -> no lookahead
    stats = r.groupby(dow).apply(lambda s: s.shift(1).expanding().mean())
    edge = stats.reset_index(level=0, drop=True).sort_index() if isinstance(stats.index, pd.MultiIndex) else stats
    edge = edge.reindex(df.index)
    sig = blank_signals(df)
    sig["entry"] = _ent(edge > thr, edge < -thr)
    sig["exit_long"] = pd.Series(True, index=df.index)     # 1-bar hold
    sig["exit_short"] = pd.Series(True, index=df.index)
    return _stops(sig, df, m)


def s095_month_of_year(df, cfg):
    m = cfg.get("stop_atr_mult", 2.0)
    df = _atr(df)
    r = df["close"].pct_change()
    mon = pd.Series(df.index.month, index=df.index)
    stats = r.groupby(mon).apply(lambda s: s.shift(1).expanding().mean())
    edge = stats.reset_index(level=0, drop=True).sort_index() if isinstance(stats.index, pd.MultiIndex) else stats
    edge = edge.reindex(df.index)
    sig = blank_signals(df)
    sig["entry"] = _ent(edge > 0, edge < 0)
    return _stops(sig, df, m)


# ════════════ S099-S102 — cycle ════════════

def s099_cycle_ma(df, cfg):
    """Fixed dominant-cycle proxy → dual MA at cycle/2 and cycle."""
    cyc = cfg.get("cycle_period", 20); m = cfg.get("stop_atr_mult", 2.0)
    df = add_sma(df, period=max(cyc // 2, 2)); df = add_sma(df, period=cyc); df = _atr(df)
    f, s = df[f"sma_{max(cyc//2,2)}"], df[f"sma_{cyc}"]
    sig = blank_signals(df)
    sig["entry"] = _ent(_xup(f, s), _xdn(f, s))
    return _stops(sig, df, m)


def s100_cycle_channel(df, cfg):
    cyc = cfg.get("cycle_period", 20); m = cfg.get("stop_atr_mult", 1.0)
    df = _atr(df)
    up = df["high"].rolling(cyc).max(); lo = df["low"].rolling(cyc).min()
    mid = (up + lo) / 2; band = (up - lo)
    c = df["close"]
    sig = blank_signals(df)
    sig["entry"] = _ent(c < lo + 0.1 * band, c > up - 0.1 * band)
    sig["exit_long"] = c > mid; sig["exit_short"] = c < mid
    return _stops(sig, df, m)


def s102_short_cycle(df, cfg):
    sc = cfg.get("short_cycle", 10); m = cfg.get("stop_atr_mult", 2.0)
    df = add_ema(df, period=max(sc // 2, 2)); df = add_ema(df, period=sc); df = _atr(df)
    osc = df[f"ema_{max(sc//2,2)}"] - df[f"ema_{sc}"]
    osc_ma = osc.ewm(span=3, adjust=False).mean()
    sig = blank_signals(df)
    sig["entry"] = _ent(_xup(osc, osc_ma) & (osc < 0), _xdn(osc, osc_ma) & (osc > 0))
    return _stops(sig, df, m)


# ════════════ S113-S117 — multiple time frames ════════════

def _htf_trend(df, mult, period):
    """Higher-timeframe EMA slope, reindexed onto the base bars (no lookahead:
    the HTF value at bar i uses only bars up to i)."""
    ema = df["close"].ewm(span=period * mult, adjust=False).mean()
    return ema


def s113_dual_timeframe(df, cfg):
    htf = cfg.get("htf_mult", 6); slow = cfg.get("htf_period", 50)
    f, s = cfg.get("fast", 10), cfg.get("slow", 20)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_ema(df, period=f); df = add_ema(df, period=s); df = _atr(df)
    trend = _htf_trend(df, htf, slow)
    up = trend > trend.shift(1)
    fa, sa = df[f"ema_{f}"], df[f"ema_{s}"]
    sig = blank_signals(df)
    sig["entry"] = _ent(_xup(fa, sa) & up, _xdn(fa, sa) & ~up)
    sig["exit_long"] = _xdn(fa, sa); sig["exit_short"] = _xup(fa, sa)
    return _stops(sig, df, m)


def s114_triple_screen(df, cfg):
    """Elder: HTF MACD direction + LTF stochastic pullback + breakout trigger."""
    htf = cfg.get("htf_mult", 5); m = cfg.get("stop_atr_mult", 2.0)
    df = _atr(df)
    e1 = df["close"].ewm(span=12 * htf, adjust=False).mean()
    e2 = df["close"].ewm(span=26 * htf, adjust=False).mean()
    macd = e1 - e2
    rising = macd > macd.shift(1)
    ll = df["low"].rolling(14).min(); hh = df["high"].rolling(14).max()
    k = (df["close"] - ll) / (hh - ll).replace(0, np.nan) * 100
    trig_up = df["high"] > df["high"].shift(1)
    trig_dn = df["low"] < df["low"].shift(1)
    sig = blank_signals(df)
    sig["entry"] = _ent(rising & (k < 30) & trig_up, ~rising & (k > 70) & trig_dn)
    sig["exit_long"] = df["close"] < df["low"].rolling(2).min().shift(1)
    sig["exit_short"] = df["close"] > df["high"].rolling(2).max().shift(1)
    return _stops(sig, df, m)


def s115_kst(df, cfg):
    m = cfg.get("stop_atr_mult", 2.0); df = _atr(df)
    c = df["close"]
    def roc(n): return (c - c.shift(n)) / c.shift(n) * 100
    kst = (roc(10).rolling(10).mean() * 1 + roc(13).rolling(13).mean() * 2 +
           roc(14).rolling(14).mean() * 3 + roc(15).rolling(9).mean() * 4)
    sgn = kst.rolling(9).mean()
    sig = blank_signals(df)
    sig["entry"] = _ent(_xup(kst, sgn) & (kst < 0), _xdn(kst, sgn) & (kst > 0))
    return _stops(sig, df, m)


def s116_bb_squeeze(df, cfg):
    p = cfg.get("bb_period", 20); m = cfg.get("stop_atr_mult", 2.0)
    df = _atr(df)
    mid = df["close"].rolling(p).mean(); sd = df["close"].rolling(p).std()
    up, lo = mid + 2 * sd, mid - 2 * sd
    width = up - lo
    squeeze = width < width.rolling(50).quantile(0.15)
    sig = blank_signals(df)
    sig["entry"] = _ent(squeeze.shift(1).fillna(False) & (df["close"] > up),
                        squeeze.shift(1).fillna(False) & (df["close"] < lo))
    sig["exit_long"] = df["close"] < mid; sig["exit_short"] = df["close"] > mid
    return _stops(sig, df, m)


def s117_three_tf(df, cfg):
    m = cfg.get("stop_atr_mult", 2.0); df = _atr(df)
    a = df["close"].ewm(span=cfg.get("p1", 20), adjust=False).mean()
    b = df["close"].ewm(span=cfg.get("p2", 60), adjust=False).mean()
    c_ = df["close"].ewm(span=cfg.get("p3", 180), adjust=False).mean()
    up = (a > a.shift(1)) & (b > b.shift(1)) & (c_ > c_.shift(1))
    dn = (a < a.shift(1)) & (b < b.shift(1)) & (c_ < c_.shift(1))
    st = pd.Series(0, index=df.index, dtype=int)
    st[up] = 1; st[dn] = -1
    sig = blank_signals(df)
    sig["entry"] = np.where((st != st.shift(1)) & (st != 0), st, 0)
    sig["exit_long"] = ~up; sig["exit_short"] = ~dn
    return _stops(sig, df, m)


# ════════════ S123-S125 — price distribution ════════════

def s125_density_sr(df, cfg):
    """Rolling-quantile support/resistance (KDE simplified to quantiles)."""
    lb = cfg.get("lookback", 50); m = cfg.get("stop_atr_mult", 1.0)
    rp = cfg.get("rsi_period", 14)
    df = add_rsi(df, period=rp); df = _atr(df)
    lo_q = df["close"].rolling(lb).quantile(0.15)
    hi_q = df["close"].rolling(lb).quantile(0.85)
    mid_q = df["close"].rolling(lb).quantile(0.50)
    c, rsi = df["close"], df[f"rsi_{rp}"]
    sig = blank_signals(df)
    sig["entry"] = _ent((c <= lo_q) & (rsi < 45), (c >= hi_q) & (rsi > 55))
    sig["exit_long"] = c >= mid_q; sig["exit_short"] = c <= mid_q
    return _stops(sig, df, m)


REGISTRY = {
    "S001_KeyReversal":  s001_002_key_reversal,
    "S003_OutsideDay":   s003_outside_day,
    "S004_InsideDay":    s004_inside_day,
    "S005_OutsideClose": s005_outside_close,
    "S006_GapFade":      s006_gap_fade,
    "S007_GapContin":    s007_gap_continuation,
    "S009_Island":       s009_island_reversal,
    "S010_Pivots":       s010_pivot_points,
    "S011_HammerEngulf": s011_hammer_engulf,
    "S012_StarEngulf":   s012_star_engulf_short,
    "S013_Doji":         s013_doji,
    "S014_Star3Bar":     s014_015_star,
    "S016_Soldiers":     s016_017_soldiers_crows,
    "S018_Harami":       s018_harami,
    "S030_SwingIndex":   s030_swing_index,
    "S041_ProjectedMA":  s041_projected_ma,
    "S042_RegSlope":     s042_reg_slope,
    "S043_ForecastOsc":  s043_forecast_osc,
    "S044_RegBandMR":    s044_reg_band_mr,
    "S049_MomOsc":       s049_mom_oscillator,
    "S053_MACDDiverg":   s053_macd_divergence,
    "S055_StochRSI":     s055_stoch_rsi,
    "S061_DblSmoothMom": s061_double_smoothed_mom,
    "S063_DivergIndex":  s063_divergence_index,
    "S064_RSIofMACD":    s064_rsi_of_macd,
    "S075_FibRetrace":   s075_fib_retracement,
    "S094_DayOfWeek":    s094_day_of_week,
    "S095_MonthOfYear":  s095_month_of_year,
    "S099_CycleMA":      s099_cycle_ma,
    "S100_CycleChannel": s100_cycle_channel,
    "S102_ShortCycle":   s102_short_cycle,
    "S113_DualTF":       s113_dual_timeframe,
    "S114_TripleScreen": s114_triple_screen,
    "S115_KST":          s115_kst,
    "S116_BBSqueeze":    s116_bb_squeeze,
    "S117_ThreeTF":      s117_three_tf,
    "S125_DensitySR":    s125_density_sr,
}

STRATEGY_CFG = {
    "S007_GapContin": {"trailing": True},
    "S016_Soldiers":  {"trailing": True},

    # ── Always-in-market crossover/state systems ─────────────────────────
    # Run flat-mode with only a stop, these hold one position for years and
    # report fake stats (1-23 trades, PF 18-inf) that are really accidental
    # buy-and-hold through the index's uptrend.
    "S030_SwingIndex":   {"mode": "reversal"},
    "S042_RegSlope":     {"mode": "reversal"},
    "S061_DblSmoothMom": {"mode": "reversal"},
    "S063_DivergIndex":  {"mode": "reversal"},
    "S099_CycleMA":      {"mode": "reversal"},
    "S102_ShortCycle":   {"mode": "reversal"},
    "S115_KST":          {"mode": "reversal"},

    # ── Pattern systems: the book gives them explicit time exits ─────────
    "S001_KeyReversal":  {"max_bars": 5},
    "S003_OutsideDay":   {"max_bars": 5},
    "S004_InsideDay":    {"max_bars": 3},
    "S013_Doji":         {"max_bars": 3},
    "S014_Star3Bar":     {"max_bars": 5},
    "S018_Harami":       {"max_bars": 5},
    "S053_MACDDiverg":   {"max_bars": 10},
    "S011_HammerEngulf": {"max_bars": 5},
    "S012_StarEngulf":   {"max_bars": 5},
    "S095_MonthOfYear":  {"max_bars": 20},
    "S094_DayOfWeek":    {"max_bars": 1},
    "S041_ProjectedMA":  {"max_bars": 5},
    "S075_FibRetrace":   {"max_bars": 20},
}
