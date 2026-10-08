import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Kaufman Batch B — Channel & Band Strategies (S019–S028)
==========================================================
S019  Linear Regression Channel Breakout
S020  Linear Regression Mean Reversion
S021  Donchian Channel Breakout (20/10)
S022  Donchian 40/20 (classic)
S023  Keltner Channel — breakout mode
S023b Keltner Channel — mean-reversion mode
S024  Bollinger Band Breakout        ⚠️ volume filter dropped (no volume data)
S025  Bollinger Band Mean Reversion (BB + RSI)
S026  ATR Band System
S027  Percentage Band System
S028  Fractal Trendline Breakout     ⚠️ simplified — see note

⚠️ Deviations forced by data / scope:
  • S024 specifies "Volume > 1.5 × avg_volume(20)" as breakout confirmation.
    Spot gold has no volume, so the filter is DROPPED. S024 is therefore a
    plain band breakout and its result is NOT a faithful test of the book rule.
  • S028 specifies a sloped trendline projected through the last two fractal
    highs/lows. Implemented instead as a breakout of the most recent CONFIRMED
    fractal level (horizontal). The confirmation delay is handled properly in
    add_fractals(); the sloped projection is not modelled.

Breakout channels are compared against the PRIOR bar's channel throughout —
comparing price to a channel that price itself is currently setting is
circular and cannot produce a valid breakout.
"""

import numpy as np
import pandas as pd

from backtesting.indicators import add_bollinger, add_rsi
from backtesting.indicators_ext import (
    add_atr, add_sma, add_donchian, add_keltner, add_linreg_channel,
    add_atr_bands, add_pct_bands, add_fractals,
)
from backtesting.book_strategies.kaufman.harness import blank_signals


def _cross_up(series: pd.Series, level: pd.Series) -> pd.Series:
    return (series.shift(1) <= level.shift(1)) & (series > level)


def _cross_dn(series: pd.Series, level: pd.Series) -> pd.Series:
    return (series.shift(1) >= level.shift(1)) & (series < level)


def _entries(long_sig: pd.Series, short_sig: pd.Series) -> np.ndarray:
    e = np.zeros(len(long_sig), dtype=int)
    ls = long_sig.fillna(False).to_numpy()
    ss = short_sig.fillna(False).to_numpy()
    e[ls] = 1
    e[ss & ~ls] = -1
    return e


def _need_atr(df, p):
    if f"atr_{p}" not in df.columns:
        df = add_atr(df, period=p)
    return df


# ── S019 — Linear Regression Channel Breakout ────────────────────────────

def s019_linreg_breakout(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    w     = cfg.get("window", 20)
    wm    = cfg.get("width_mult", 2.0)
    atr_p = cfg.get("atr_period", 14)

    df = add_linreg_channel(df, window=w, width_mult=wm)
    df = _need_atr(df, atr_p)

    close = df["close"]
    up, lo, mid = df[f"lr_upper_{w}"], df[f"lr_lower_{w}"], df[f"lr_mid_{w}"]
    slope = df[f"lr_slope_{w}"]

    sig = blank_signals(df)
    sig["entry"] = _entries(_cross_up(close, up) & (slope > 0),
                            _cross_dn(close, lo) & (slope < 0))
    sig["exit_long"]  = close < mid
    sig["exit_short"] = close > mid
    sig["stop_long"]  = lo
    sig["stop_short"] = up
    return sig


# ── S020 — Linear Regression Mean Reversion ──────────────────────────────

def s020_linreg_meanrev(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    w      = cfg.get("window", 20)
    wm     = cfg.get("width_mult", 2.0)
    trend_p = cfg.get("trend_period", 50)
    atr_p  = cfg.get("atr_period", 14)

    df = add_linreg_channel(df, window=w, width_mult=wm)
    df = add_sma(df, period=trend_p)
    df = _need_atr(df, atr_p)

    close = df["close"]
    up, lo, mid = df[f"lr_upper_{w}"], df[f"lr_lower_{w}"], df[f"lr_mid_{w}"]
    sma = df[f"sma_{trend_p}"]
    atr = df[f"atr_{atr_p}"]
    uptrend = sma > sma.shift(1)

    sig = blank_signals(df)
    # Fade the band edge, but only WITH the longer-term trend
    sig["entry"] = _entries((close < lo) & uptrend,
                            (close > up) & ~uptrend)
    sig["exit_long"]  = close >= mid
    sig["exit_short"] = close <= mid
    sig["stop_long"]  = lo - atr
    sig["stop_short"] = up + atr
    return sig


# ── S021 / S022 — Donchian breakouts ─────────────────────────────────────

def _donchian_breakout(df, cfg, entry_p, exit_p):
    atr_p = cfg.get("atr_period", 14)
    smult = cfg.get("stop_atr_mult", 2.0)

    df = add_donchian(df, period=entry_p)
    df = add_donchian(df, period=exit_p)
    df = _need_atr(df, atr_p)

    close = df["close"]
    atr   = df[f"atr_{atr_p}"]
    # PRIOR bar's channel — the current bar helps set the channel it would
    # otherwise be "breaking", which is circular
    up_e = df[f"dc_upper_{entry_p}"].shift(1)
    lo_e = df[f"dc_lower_{entry_p}"].shift(1)
    up_x = df[f"dc_upper_{exit_p}"].shift(1)
    lo_x = df[f"dc_lower_{exit_p}"].shift(1)

    sig = blank_signals(df)
    sig["entry"] = _entries(close > up_e, close < lo_e)
    sig["exit_long"]  = close < lo_x
    sig["exit_short"] = close > up_x
    sig["stop_long"]  = close - smult * atr
    sig["stop_short"] = close + smult * atr
    return sig


def s021_donchian_20_10(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    return _donchian_breakout(df, cfg, cfg.get("entry_period", 20), cfg.get("exit_period", 10))


def s022_donchian_40_20(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    return _donchian_breakout(df, cfg, cfg.get("entry_period", 40), cfg.get("exit_period", 20))


# ── S023 — Keltner (two modes) ───────────────────────────────────────────

def s023_keltner_breakout(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    ema_p = cfg.get("ema_period", 20)
    atr_p = cfg.get("atr_period", 14)
    mult  = cfg.get("mult", 2.0)
    smult = cfg.get("stop_atr_mult", 2.0)
    trail_n = cfg.get("trail_bars", 20)

    df = add_keltner(df, ema_period=ema_p, atr_period=atr_p, mult=mult)
    atr = df[f"atr_{atr_p}"]
    close = df["close"]
    up, lo, mid = (df[f"kc_upper_{ema_p}"], df[f"kc_lower_{ema_p}"], df[f"kc_mid_{ema_p}"])

    sig = blank_signals(df)
    sig["entry"] = _entries(_cross_up(close, up), _cross_dn(close, lo))
    # Book: "Breakout: trailing ATR stop" — chandelier, needs cfg trailing=True
    sig["stop_long"]  = df["high"].rolling(trail_n, min_periods=1).max() - smult * atr
    sig["stop_short"] = df["low"].rolling(trail_n, min_periods=1).min() + smult * atr
    return sig


def s023b_keltner_meanrev(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    ema_p = cfg.get("ema_period", 20)
    atr_p = cfg.get("atr_period", 14)
    mult  = cfg.get("mult", 2.0)
    smult = cfg.get("stop_atr_mult", 1.0)

    df = add_keltner(df, ema_period=ema_p, atr_period=atr_p, mult=mult)
    atr = df[f"atr_{atr_p}"]
    close = df["close"]
    up, lo, mid = (df[f"kc_upper_{ema_p}"], df[f"kc_lower_{ema_p}"], df[f"kc_mid_{ema_p}"])

    sig = blank_signals(df)
    sig["entry"] = _entries(close < lo, close > up)
    sig["exit_long"]  = close >= mid
    sig["exit_short"] = close <= mid
    sig["stop_long"]  = df["low"] - smult * atr
    sig["stop_short"] = df["high"] + smult * atr
    return sig


# ── S024 — Bollinger Breakout (volume filter dropped) ────────────────────

def s024_bb_breakout(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    p     = cfg.get("bb_period", 20)
    sd    = cfg.get("bb_std", 2.0)
    atr_p = cfg.get("atr_period", 14)

    df = add_bollinger(df, period=p, std_dev=sd)
    df = _need_atr(df, atr_p)

    close = df["close"]
    up, lo, mid = df[f"bb_upper_{p}"], df[f"bb_lower_{p}"], df[f"bb_middle_{p}"]

    sig = blank_signals(df)
    sig["entry"] = _entries(_cross_up(close, up), _cross_dn(close, lo))
    sig["exit_long"]  = close < mid
    sig["exit_short"] = close > mid
    sig["stop_long"]  = lo
    sig["stop_short"] = up
    return sig


# ── S025 — Bollinger Mean Reversion + RSI ────────────────────────────────

def s025_bb_meanrev(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    p      = cfg.get("bb_period", 20)
    sd     = cfg.get("bb_std", 2.0)
    rsi_p  = cfg.get("rsi_period", 14)
    os_l   = cfg.get("rsi_os", 35)
    ob_l   = cfg.get("rsi_ob", 65)
    atr_p  = cfg.get("atr_period", 14)
    smult  = cfg.get("stop_atr_mult", 0.5)

    df = add_bollinger(df, period=p, std_dev=sd)
    df = add_rsi(df, period=rsi_p)
    df = _need_atr(df, atr_p)

    close = df["close"]
    up, lo, mid = df[f"bb_upper_{p}"], df[f"bb_lower_{p}"], df[f"bb_middle_{p}"]
    rsi = df[f"rsi_{rsi_p}"]
    atr = df[f"atr_{atr_p}"]

    sig = blank_signals(df)
    sig["entry"] = _entries((close < lo) & (rsi < os_l),
                            (close > up) & (rsi > ob_l))
    sig["exit_long"]  = close >= mid
    sig["exit_short"] = close <= mid
    sig["stop_long"]  = df["low"] - smult * atr
    sig["stop_short"] = df["high"] + smult * atr
    return sig


# ── S026 / S027 — ATR and Percentage bands ───────────────────────────────

def s026_atr_bands(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    ma_p  = cfg.get("ma_period", 20)
    atr_p = cfg.get("atr_period", 14)
    mult  = cfg.get("band_mult", 2.5)

    df = add_atr_bands(df, ma_period=ma_p, atr_period=atr_p, mult=mult)
    close = df["close"]
    up, lo, mid = (df[f"atrb_upper_{ma_p}"], df[f"atrb_lower_{ma_p}"], df[f"atrb_mid_{ma_p}"])

    sig = blank_signals(df)
    sig["entry"] = _entries(_cross_up(close, up), _cross_dn(close, lo))
    sig["exit_long"]  = close < mid
    sig["exit_short"] = close > mid
    sig["stop_long"]  = lo
    sig["stop_short"] = up
    return sig


def s027_pct_bands(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    ma_p = cfg.get("ma_period", 20)
    pct  = cfg.get("band_pct", 0.02)

    df = add_pct_bands(df, ma_period=ma_p, pct=pct)
    close = df["close"]
    up, lo, mid = (df[f"pctb_upper_{ma_p}"], df[f"pctb_lower_{ma_p}"], df[f"pctb_mid_{ma_p}"])

    sig = blank_signals(df)
    sig["entry"] = _entries(_cross_up(close, up), _cross_dn(close, lo))
    sig["exit_long"]  = close < mid
    sig["exit_short"] = close > mid
    sig["stop_long"]  = lo
    sig["stop_short"] = up
    return sig


# ── S028 — Fractal breakout (simplified) ─────────────────────────────────

def s028_fractal_breakout(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    n     = cfg.get("fractal_n", 2)
    atr_p = cfg.get("atr_period", 14)
    smult = cfg.get("stop_atr_mult", 0.5)

    df = add_fractals(df, n=n)
    df = _need_atr(df, atr_p)

    close = df["close"]
    fh, fl = df[f"frac_high_{n}"], df[f"frac_low_{n}"]
    atr = df[f"atr_{atr_p}"]

    sig = blank_signals(df)
    sig["entry"] = _entries(_cross_up(close, fh), _cross_dn(close, fl))
    sig["exit_long"]  = close < fl
    sig["exit_short"] = close > fh
    sig["stop_long"]  = fl - smult * atr
    sig["stop_short"] = fh + smult * atr
    return sig


REGISTRY = {
    "S019_LinRegBreak":  s019_linreg_breakout,
    "S020_LinRegMR":     s020_linreg_meanrev,
    "S021_Donchian2010": s021_donchian_20_10,
    "S022_Donchian4020": s022_donchian_40_20,
    "S023_KeltnerBreak": s023_keltner_breakout,
    "S023b_KeltnerMR":   s023b_keltner_meanrev,
    "S024_BBBreak":      s024_bb_breakout,
    "S025_BBMeanRev":    s025_bb_meanrev,
    "S026_ATRBands":     s026_atr_bands,
    "S027_PctBands":     s027_pct_bands,
    "S028_Fractal":      s028_fractal_breakout,
}

# Per-strategy harness overrides (time exits, trailing stops)
STRATEGY_CFG = {
    "S020_LinRegMR":     {"max_bars": 10},
    "S023_KeltnerBreak": {"trailing": True},
    "S025_BBMeanRev":    {"max_bars": 8},
}
