import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Kaufman Batches A / D / E / F — MA systems, volatility, oscillators, swing
=============================================================================
A  S033-S041  moving-average systems
D  S118,S120-S122  volatility breakout + regime filters
E  S048-S064  momentum / oscillators
F  S029-S032  swing & Turtle-style breakout

Volume-dependent strategies (S065-S074) are excluded — XAUUSD has no volume.
Mean-reversion variants are still included so the pattern can be re-tested,
but the expectation from Batches B/C is that they fail.
"""

import numpy as np
import pandas as pd

from backtesting.indicators import add_ema, add_rsi
from backtesting.indicators_ext import (
    add_atr, add_sma, add_roc, add_momentum, add_cmo, add_macd, add_stochastic,
    add_williams_r, add_cci, add_trix, add_hull, add_psar, add_ultimate_osc,
    add_swings, add_donchian, add_efficiency_ratio, add_linreg_channel,
)
from backtesting.book_strategies.kaufman.harness import blank_signals


def _atr(df, p=14):
    if f"atr_{p}" not in df.columns:
        df = add_atr(df, period=p)
    return df


def _entries(long_sig, short_sig):
    e = np.zeros(len(long_sig), dtype=int)
    ls = long_sig.fillna(False).to_numpy()
    ss = short_sig.fillna(False).to_numpy()
    e[ls] = 1
    e[ss & ~ls] = -1
    return e


def _xup(a, b):
    return (a.shift(1) <= b.shift(1)) & (a > b)


def _xdn(a, b):
    return (a.shift(1) >= b.shift(1)) & (a < b)


def _state_trans(long_state, short_state):
    st = pd.Series(0, index=long_state.index, dtype=int)
    st[long_state.fillna(False)] = 1
    st[short_state.fillna(False)] = -1
    return np.where((st != st.shift(1)) & (st != 0), st, 0)


def _atr_stops(sig, df, mult, p=14):
    atr = df[f"atr_{p}"]
    sig["stop_long"]  = df["close"] - mult * atr
    sig["stop_short"] = df["close"] + mult * atr
    return sig


# ══════════ BATCH A — Moving-average systems (S033-S041) ══════════

def s033_single_ma(df, cfg):
    p = cfg.get("ma_period", 20); m = cfg.get("stop_atr_mult", 2.0)
    df = add_ema(df, period=p); df = _atr(df)
    ma, c = df[f"ema_{p}"], df["close"]
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(c, ma), _xdn(c, ma))
    return _atr_stops(sig, df, m)


def s034_dual_ma(df, cfg):
    f, s = cfg.get("fast_period", 10), cfg.get("slow_period", 30)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_ema(df, period=f); df = add_ema(df, period=s); df = _atr(df)
    fa, sa = df[f"ema_{f}"], df[f"ema_{s}"]
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(fa, sa), _xdn(fa, sa))
    return _atr_stops(sig, df, m)


def s035_triple_ma(df, cfg):
    f, mid, s = cfg.get("fast", 5), cfg.get("mid", 13), cfg.get("slow", 34)
    m = cfg.get("stop_atr_mult", 2.0)
    for p in (f, mid, s):
        df = add_ema(df, period=p)
    df = _atr(df)
    fa, ma_, sa = df[f"ema_{f}"], df[f"ema_{mid}"], df[f"ema_{s}"]
    sig = blank_signals(df)
    sig["entry"] = _entries((fa > ma_) & (ma_ > sa) & _xup(fa, ma_),
                            (fa < ma_) & (ma_ < sa) & _xdn(fa, ma_))
    sig["exit_long"]  = _xdn(fa, ma_)
    sig["exit_short"] = _xup(fa, ma_)
    return _atr_stops(sig, df, m)


def s036_ma_slope(df, cfg):
    p, sp = cfg.get("ma_period", 20), cfg.get("slope_period", 5)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_ema(df, period=p); df = _atr(df)
    ma = df[f"ema_{p}"]
    slope = ma - ma.shift(sp)
    z = pd.Series(0.0, index=df.index)
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(slope, z), _xdn(slope, z))
    return _atr_stops(sig, df, m)


def s037_psar(df, cfg):
    df = add_psar(df, af_start=cfg.get("af_start", 0.02),
                  af_step=cfg.get("af_step", 0.02), af_max=cfg.get("af_max", 0.20))
    df = _atr(df)
    d = df["psar_dir"]
    sig = blank_signals(df)
    sig["entry"] = _entries((d == 1) & (d.shift(1) == -1), (d == -1) & (d.shift(1) == 1))
    sig["stop_long"]  = df["psar"]
    sig["stop_short"] = df["psar"]
    return sig


def s038_trix(df, cfg):
    p, s = cfg.get("trix_period", 15), cfg.get("signal_period", 9)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_trix(df, period=p, signal=s); df = _atr(df)
    t, sg = df["trix"], df["trix_signal"]
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(t, sg), _xdn(t, sg))
    return _atr_stops(sig, df, m)


def s039_ma_sequence(df, cfg):
    m = cfg.get("stop_atr_mult", 2.0)
    for p in (5, 10, 20, 50):
        df = add_ema(df, period=p)
    df = _atr(df)
    a, b, c_, d_ = df["ema_5"], df["ema_10"], df["ema_20"], df["ema_50"]
    bull = (a > b) & (b > c_) & (c_ > d_)
    bear = (a < b) & (b < c_) & (c_ < d_)
    sig = blank_signals(df)
    sig["entry"] = _state_trans(bull, bear)
    sig["exit_long"]  = _xdn(a, b)
    sig["exit_short"] = _xup(a, b)
    return _atr_stops(sig, df, m)


def s040_hull(df, cfg):
    p = cfg.get("hma_period", 20); m = cfg.get("stop_atr_mult", 2.0)
    df = add_hull(df, period=p); df = _atr(df)
    h = df[f"hma_{p}"]
    sig = blank_signals(df)
    sig["entry"] = _state_trans(h > h.shift(1), h < h.shift(1))
    return _atr_stops(sig, df, m)


# ══════════ BATCH D — Volatility (S118, S120-S122) ══════════

def s118_atr_vol_breakout(df, cfg):
    ap = cfg.get("atr_period", 14)
    em = cfg.get("atr_mult_entry", 1.5)
    sm = cfg.get("atr_mult_stop", 2.0)
    tp = cfg.get("trend_ma", 50)
    df = _atr(df, ap); df = add_ema(df, period=tp)
    atr, c, tr = df[f"atr_{ap}"], df["close"], df[f"ema_{tp}"]
    up = c.shift(1) + em * atr
    lo = c.shift(1) - em * atr
    sig = blank_signals(df)
    sig["entry"] = _entries((c > up) & (c > tr), (c < lo) & (c < tr))
    trail = cfg.get("trail_bars", 20)
    sig["stop_long"]  = df["high"].rolling(trail, min_periods=1).max() - sm * atr
    sig["stop_short"] = df["low"].rolling(trail, min_periods=1).min() + sm * atr
    return sig


def s120_vol_regime_breakout(df, cfg):
    """S120 regime switch, trend-following leg only (breakout in low vol)."""
    ap = cfg.get("atr_period", 14)
    lookback = cfg.get("regime_lookback", 252)
    thresh = cfg.get("low_vol_pct", 0.40)
    ep = cfg.get("entry_period", 20)
    m = cfg.get("stop_atr_mult", 2.0)
    df = _atr(df, ap); df = add_donchian(df, period=ep)
    atr = df[f"atr_{ap}"]
    pct = atr.rolling(lookback).rank(pct=True)
    c = df["close"]
    up, lo = df[f"dc_upper_{ep}"].shift(1), df[f"dc_lower_{ep}"].shift(1)
    low_vol = pct <= thresh
    sig = blank_signals(df)
    sig["entry"] = _entries((c > up) & low_vol, (c < lo) & low_vol)
    return _atr_stops(sig, df, m, ap)


def s121_noise_filter_trend(df, cfg):
    """S121 signal-to-noise filter applied to a dual-MA trend system."""
    lb = cfg.get("lookback", 20)
    thr = cfg.get("snr_threshold", 0.20)
    f, s = cfg.get("fast_period", 10), cfg.get("slow_period", 30)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_ema(df, period=f); df = add_ema(df, period=s); df = _atr(df)
    noise = (df["high"] - df["low"]).rolling(lb).mean()
    signal_move = (df["close"] - df["close"].shift(lb)).abs()
    snr = signal_move / (lb * noise)
    fa, sa = df[f"ema_{f}"], df[f"ema_{s}"]
    ok = snr >= thr
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(fa, sa) & ok, _xdn(fa, sa) & ok)
    return _atr_stops(sig, df, m)


def s091_er_trend(df, cfg):
    """S091 efficiency-ratio filter on a dual-MA trend system."""
    er_p = cfg.get("er_period", 10)
    thr  = cfg.get("er_threshold", 0.30)
    f, s = cfg.get("fast_period", 10), cfg.get("slow_period", 30)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_efficiency_ratio(df, period=er_p)
    df = add_ema(df, period=f); df = add_ema(df, period=s); df = _atr(df)
    fa, sa = df[f"ema_{f}"], df[f"ema_{s}"]
    ok = df[f"er_{er_p}"] >= thr
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(fa, sa) & ok, _xdn(fa, sa) & ok)
    return _atr_stops(sig, df, m)


# ══════════ BATCH E — Oscillators (S048-S064) ══════════

def s048_momentum(df, cfg):
    p = cfg.get("mom_period", 12); m = cfg.get("stop_atr_mult", 2.0)
    df = add_momentum(df, period=p); df = _atr(df)
    mom = df[f"mom_{p}"]
    z = pd.Series(0.0, index=df.index)
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(mom, z), _xdn(mom, z))
    return _atr_stops(sig, df, m)


def s050_rsi_trend(df, cfg):
    rp = cfg.get("rsi_period", 14); os_l = cfg.get("os_level", 30)
    ob_l = cfg.get("ob_level", 70); tp = cfg.get("trend_ma", 50)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_rsi(df, period=rp); df = add_sma(df, period=tp); df = _atr(df)
    r, c, tr = df[f"rsi_{rp}"], df["close"], df[f"sma_{tp}"]
    lvl_o = pd.Series(float(os_l), index=df.index)
    lvl_b = pd.Series(float(ob_l), index=df.index)
    fifty = pd.Series(50.0, index=df.index)
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(r, lvl_o) & (c > tr), _xdn(r, lvl_b) & (c < tr))
    sig["exit_long"]  = r > 50
    sig["exit_short"] = r < 50
    return _atr_stops(sig, df, m)


def s051_rsi2(df, cfg):
    tp = cfg.get("trend_ma", 200); m = cfg.get("stop_atr_mult", 2.0)
    df = add_rsi(df, period=2); df = add_sma(df, period=tp); df = _atr(df)
    r, c, tr = df["rsi_2"], df["close"], df[f"sma_{tp}"]
    sig = blank_signals(df)
    sig["entry"] = _entries((c > tr) & (r < cfg.get("entry_long", 10)),
                            (c < tr) & (r > cfg.get("entry_short", 90)))
    sig["exit_long"]  = r > cfg.get("exit_long", 65)
    sig["exit_short"] = r < cfg.get("exit_short", 35)
    return _atr_stops(sig, df, m)


def s052_macd(df, cfg):
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_macd(df, fast=cfg.get("fast", 12), slow=cfg.get("slow", 26),
                  signal=cfg.get("signal", 9))
    df = _atr(df)
    ml, sl = df["macd"], df["macd_signal"]
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(ml, sl), _xdn(ml, sl))
    return _atr_stops(sig, df, m)


def s054_stochastic(df, cfg):
    m = cfg.get("stop_atr_mult", 2.0); tp = cfg.get("trend_ma", 50)
    df = add_stochastic(df, k_period=cfg.get("k_period", 14))
    df = add_ema(df, period=tp); df = _atr(df)
    k, d, c, tr = df["stoch_k"], df["stoch_d"], df["close"], df[f"ema_{tp}"]
    sig = blank_signals(df)
    sig["entry"] = _entries((k < cfg.get("os", 20)) & _xup(k, d) & (c > tr),
                            (k > cfg.get("ob", 80)) & _xdn(k, d) & (c < tr))
    sig["exit_long"]  = k > 70
    sig["exit_short"] = k < 30
    return _atr_stops(sig, df, m)


def s056_williams_r(df, cfg):
    p = cfg.get("wr_period", 14); m = cfg.get("stop_atr_mult", 2.0)
    df = add_williams_r(df, period=p); df = _atr(df)
    w = df[f"willr_{p}"]
    os_l = pd.Series(float(cfg.get("os", -80)), index=df.index)
    ob_l = pd.Series(float(cfg.get("ob", -20)), index=df.index)
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(w, os_l), _xdn(w, ob_l))
    sig["exit_long"]  = w > -50
    sig["exit_short"] = w < -50
    return _atr_stops(sig, df, m)


def s057_willr_trend(df, cfg):
    p = cfg.get("wr_period", 14); tp = cfg.get("trend_ma", 50)
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_williams_r(df, period=p); df = add_ema(df, period=tp); df = _atr(df)
    w, c, tr = df[f"willr_{p}"], df["close"], df[f"ema_{tp}"]
    sig = blank_signals(df)
    sig["entry"] = _entries((w < -80) & (c > tr), (w > -20) & (c < tr))
    return _atr_stops(sig, df, m)


def s059_cmo(df, cfg):
    p = cfg.get("cmo_period", 14); m = cfg.get("stop_atr_mult", 2.0)
    df = add_cmo(df, period=p); df = _atr(df)
    cm = df[f"cmo_{p}"]
    os_l = pd.Series(float(cfg.get("os", -50)), index=df.index)
    ob_l = pd.Series(float(cfg.get("ob", 50)), index=df.index)
    z = pd.Series(0.0, index=df.index)
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(cm, os_l), _xdn(cm, ob_l))
    sig["exit_long"]  = _xdn(cm, z)
    sig["exit_short"] = _xup(cm, z)
    return _atr_stops(sig, df, m)


def s060_cci_trend(df, cfg):
    p = cfg.get("cci_period", 20); m = cfg.get("stop_atr_mult", 2.0)
    df = add_cci(df, period=p); df = _atr(df)
    cc = df[f"cci_{p}"]
    hi = pd.Series(100.0, index=df.index)
    lo = pd.Series(-100.0, index=df.index)
    z  = pd.Series(0.0, index=df.index)
    sig = blank_signals(df)
    # Trend interpretation: crossing OUT of the band starts a trend
    sig["entry"] = _entries(_xup(cc, hi), _xdn(cc, lo))
    sig["exit_long"]  = _xdn(cc, z)
    sig["exit_short"] = _xup(cc, z)
    return _atr_stops(sig, df, m)


def s058_ultimate(df, cfg):
    m = cfg.get("stop_atr_mult", 2.0)
    df = add_ultimate_osc(df); df = _atr(df)
    u = df["uo"]
    os_l = pd.Series(float(cfg.get("os", 30)), index=df.index)
    ob_l = pd.Series(float(cfg.get("ob", 70)), index=df.index)
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(u, os_l), _xdn(u, ob_l))
    sig["exit_long"]  = u > 65
    sig["exit_short"] = u < 35
    return _atr_stops(sig, df, m)


def s062_velocity_accel(df, cfg):
    p = cfg.get("ma_period", 20); m = cfg.get("stop_atr_mult", 2.0)
    df = add_ema(df, period=p); df = _atr(df)
    ma = df[f"ema_{p}"]
    vel = ma.diff()
    acc = vel.diff()
    sig = blank_signals(df)
    sig["entry"] = _state_trans((vel > 0) & (acc > 0), (vel < 0) & (acc < 0))
    sig["exit_long"]  = acc < 0
    sig["exit_short"] = acc > 0
    return _atr_stops(sig, df, m)


# ══════════ BATCH F — Swing & Turtle (S029-S032) ══════════

def s029_swing(df, cfg):
    pct = cfg.get("swing_pct", 0.03); m = cfg.get("stop_atr_mult", 0.5)
    df = add_swings(df, pct=pct); df = _atr(df)
    c, sh, sl = df["close"], df["swing_high"], df["swing_low"]
    atr = df["atr_14"]
    sig = blank_signals(df)
    sig["entry"] = _entries(_xup(c, sh), _xdn(c, sl))
    sig["stop_long"]  = sl - m * atr
    sig["stop_short"] = sh + m * atr
    return sig


def s031_turtle(df, cfg):
    """Turtle System 1/2 breakout — no pyramiding (harness is single-unit)."""
    ep = cfg.get("entry_period", 20); xp = cfg.get("exit_period", 10)
    m  = cfg.get("stop_atr_mult", 2.0); ap = cfg.get("atr_period", 20)
    df = add_donchian(df, period=ep); df = add_donchian(df, period=xp)
    df = _atr(df, ap)
    c = df["close"]; atr = df[f"atr_{ap}"]
    up, lo = df[f"dc_upper_{ep}"].shift(1), df[f"dc_lower_{ep}"].shift(1)
    xu, xl = df[f"dc_upper_{xp}"].shift(1), df[f"dc_lower_{xp}"].shift(1)
    sig = blank_signals(df)
    sig["entry"] = _entries(c > up, c < lo)
    sig["exit_long"]  = c < xl
    sig["exit_short"] = c > xu
    sig["stop_long"]  = c - m * atr
    sig["stop_short"] = c + m * atr
    return sig


def s032_dbs(df, cfg):
    """Dynamic Breakout System — stdev channel around a rolling mean."""
    lb = cfg.get("lookback", 20); sm = cfg.get("std_mult", 1.0)
    m = cfg.get("stop_atr_mult", 2.0)
    df = _atr(df)
    mean = df["close"].rolling(lb).mean()
    sd = df["close"].rolling(lb).std()
    up, lo = (mean + sm * sd).shift(1), (mean - sm * sd).shift(1)
    c = df["close"]
    sig = blank_signals(df)
    sig["entry"] = _entries(c > up, c < lo)
    sig["exit_long"]  = c < lo
    sig["exit_short"] = c > up
    return _atr_stops(sig, df, m)


REGISTRY = {
    # Batch A
    "S033_SingleMA":   s033_single_ma,
    "S034_DualMA":     s034_dual_ma,
    "S035_TripleMA":   s035_triple_ma,
    "S036_MASlope":    s036_ma_slope,
    "S037_PSAR":       s037_psar,
    "S038_TRIX":       s038_trix,
    "S039_MASequence": s039_ma_sequence,
    "S040_Hull":       s040_hull,
    # Batch D
    "S118_ATRVolBrk":  s118_atr_vol_breakout,
    "S120_VolRegime":  s120_vol_regime_breakout,
    "S121_NoiseFilter": s121_noise_filter_trend,
    "S091_ERTrend":    s091_er_trend,
    # Batch E
    "S048_Momentum":   s048_momentum,
    "S050_RSITrend":   s050_rsi_trend,
    "S051_RSI2":       s051_rsi2,
    "S052_MACD":       s052_macd,
    "S054_Stochastic": s054_stochastic,
    "S056_WilliamsR":  s056_williams_r,
    "S057_WillRTrend": s057_willr_trend,
    "S058_Ultimate":   s058_ultimate,
    "S059_CMO":        s059_cmo,
    "S060_CCI":        s060_cci_trend,
    "S062_VelAccel":   s062_velocity_accel,
    # Batch F
    "S029_Swing":      s029_swing,
    "S031_Turtle":     s031_turtle,
    "S032_DBS":        s032_dbs,
}

STRATEGY_CFG = {
    "S118_ATRVolBrk": {"trailing": True},

    # ── Always-in-market reversal systems ────────────────────────────────
    # The book specifies "Exit: Reversal" for these — the opposite signal IS
    # the exit. Run flat-mode with only a stop, they hold a single position
    # for years and report absurd stats (5-11 trades, PF 8-46) that are just
    # accidental buy-and-hold, exactly like the S092 bug.
    "S033_SingleMA":    {"mode": "reversal"},
    "S034_DualMA":      {"mode": "reversal"},
    "S036_MASlope":     {"mode": "reversal"},
    "S037_PSAR":        {"mode": "reversal"},
    "S038_TRIX":        {"mode": "reversal"},
    "S040_Hull":        {"mode": "reversal"},
    "S048_Momentum":    {"mode": "reversal"},
    "S052_MACD":        {"mode": "reversal"},
    "S091_ERTrend":     {"mode": "reversal"},
    "S121_NoiseFilter": {"mode": "reversal"},
    "S120_VolRegime":   {"mode": "reversal"},
    "S032_DBS":         {"mode": "reversal"},
}
