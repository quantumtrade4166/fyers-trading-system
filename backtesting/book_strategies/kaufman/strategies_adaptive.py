import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Kaufman Batch C — Adaptive Strategies (S088–S092)
=====================================================
S088  KAMA — Kaufman's Adaptive Moving Average
S089  VIDYA — Chande's Variable Index Dynamic Average
S090  Adaptive RSI / Dynamic Momentum Index
S091  Efficiency Ratio trend filter  (an OVERLAY, applied via cfg["er_filter"])
S092  Adaptive Intraday Breakout (Meyers)

Each strategy is a signal function for harness.py — see that module's
docstring for the contract.

⚠️ Deviations from the book, forced by our data (XAUUSD OHLC, no volume):
  • S092 specifies VWAP as its dynamic stop. VWAP requires volume, which
    spot gold data does not have — substituted an ATR stop and flagged.
  • S092's published formula writes nlr = (hr - Low)/…, using the *high*
    reference for the low range. Read as a transcription slip; implemented
    with lr (the low of the same reference bar), which is the symmetric
    reading. Noted so the deviation is visible in results.
"""

import numpy as np
import pandas as pd

from backtesting.indicators_ext import (
    add_atr, add_kama, add_vidya, add_adaptive_rsi, add_efficiency_ratio,
)
from backtesting.book_strategies.kaufman.harness import blank_signals


def _apply_er_filter(sig: pd.DataFrame, df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    S091 overlay — suppress entries when the market is too choppy.
    Only entries are filtered; exits always remain active so an open
    position can still be closed.
    """
    thr = cfg.get("er_filter")
    if not thr:
        return sig
    period = cfg.get("er_filter_period", 10)
    col = f"er_{period}"
    if col not in df.columns:
        df = add_efficiency_ratio(df, period=period)
    ok = (df[col] >= thr).fillna(False).to_numpy()
    sig.loc[~ok, "entry"] = 0
    return sig


def _state_transitions(long_state: pd.Series, short_state: pd.Series) -> np.ndarray:
    """
    Convert persistent long/short states into entry signals fired only on
    the bar the state changes — avoids re-entry spam after a stop-out.
    """
    state = pd.Series(0, index=long_state.index, dtype=int)
    state[long_state.fillna(False)] = 1
    state[short_state.fillna(False)] = -1
    changed = state != state.shift(1)
    return np.where(changed & (state != 0), state, 0)


# ── S088 — KAMA ──────────────────────────────────────────────────────────

def s088_kama(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Long : KAMA slope positive AND Close > KAMA
    Short: KAMA slope negative AND Close < KAMA
    Exit : KAMA slope reverses
    Stop : KAMA ∓ stop_atr_mult × ATR
    """
    er_p  = cfg.get("er_period", 10)
    fast  = cfg.get("fast", 2)
    slow  = cfg.get("slow", 30)
    atr_p = cfg.get("atr_period", 14)
    smult = cfg.get("stop_atr_mult", 2.0)

    kcol = f"kama_{er_p}_{fast}_{slow}"
    if kcol not in df.columns:
        df = add_kama(df, er_period=er_p, fast=fast, slow=slow)
    acol = f"atr_{atr_p}"
    if acol not in df.columns:
        df = add_atr(df, period=atr_p)

    kama, atr, close = df[kcol], df[acol], df["close"]
    rising  = kama > kama.shift(1)
    falling = kama < kama.shift(1)

    sig = blank_signals(df)
    sig["entry"] = _state_transitions(rising & (close > kama),
                                      falling & (close < kama))
    sig["exit_long"]  = falling
    sig["exit_short"] = rising
    sig["stop_long"]  = kama - smult * atr
    sig["stop_short"] = kama + smult * atr
    return _apply_er_filter(sig, df, cfg)


# ── S089 — VIDYA ─────────────────────────────────────────────────────────

def s089_vidya(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Long : VIDYA rising AND Close > VIDYA
    Short: VIDYA falling AND Close < VIDYA
    Exit : VIDYA slope reverses
    Stop : VIDYA ∓ stop_atr_mult × ATR
    """
    short_p = cfg.get("short_period", 9)
    cmo_p   = cfg.get("cmo_period", 14)
    atr_p   = cfg.get("atr_period", 14)
    smult   = cfg.get("stop_atr_mult", 2.0)

    vcol = f"vidya_{short_p}_{cmo_p}"
    if vcol not in df.columns:
        df = add_vidya(df, short_period=short_p, cmo_period=cmo_p)
    acol = f"atr_{atr_p}"
    if acol not in df.columns:
        df = add_atr(df, period=atr_p)

    vidya, atr, close = df[vcol], df[acol], df["close"]
    rising  = vidya > vidya.shift(1)
    falling = vidya < vidya.shift(1)

    sig = blank_signals(df)
    sig["entry"] = _state_transitions(rising & (close > vidya),
                                      falling & (close < vidya))
    sig["exit_long"]  = falling
    sig["exit_short"] = rising
    sig["stop_long"]  = vidya - smult * atr
    sig["stop_short"] = vidya + smult * atr
    return _apply_er_filter(sig, df, cfg)


# ── S090 — Adaptive RSI (Dynamic Momentum Index) ─────────────────────────

def s090_adaptive_rsi(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Long : DMI crosses up through os_level
    Short: DMI crosses down through ob_level
    Exit : DMI back through 50
    Stop : entry ∓ stop_atr_mult × ATR (harness fallback)
    """
    base  = cfg.get("base_period", 14)
    os_l  = cfg.get("os_level", 30)
    ob_l  = cfg.get("ob_level", 70)
    atr_p = cfg.get("atr_period", 14)
    smult = cfg.get("stop_atr_mult", 2.0)

    dcol = f"dmi_{base}"
    if dcol not in df.columns:
        df = add_adaptive_rsi(df, base_period=base)
    acol = f"atr_{atr_p}"
    if acol not in df.columns:
        df = add_atr(df, period=atr_p)

    dmi, atr, close = df[dcol], df[acol], df["close"]
    prev = dmi.shift(1)

    cross_up_os   = (prev <= os_l) & (dmi > os_l)
    cross_dn_ob   = (prev >= ob_l) & (dmi < ob_l)

    sig = blank_signals(df)
    entry = np.zeros(len(df), dtype=int)
    entry[cross_up_os.fillna(False).to_numpy()] = 1
    entry[cross_dn_ob.fillna(False).to_numpy()] = -1
    sig["entry"] = entry

    sig["exit_long"]  = (prev <= 50) & (dmi > 50)
    sig["exit_short"] = (prev >= 50) & (dmi < 50)
    sig["stop_long"]  = close - smult * atr
    sig["stop_short"] = close + smult * atr
    return _apply_er_filter(sig, df, cfg)


# ── S092 — Adaptive Intraday Breakout (Meyers) ───────────────────────────

def s092_adaptive_breakout(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Normalised range breakout:
        hr, lr  = High/Low of the bar n_bars ago (a specific bar, not a max)
        nhr     = (High - hr) / (ATR × n_bars^range_power)
        nlr     = (lr - Low)  / (ATR × n_bars^range_power)
        Long    when nhr >= rolling max of nhr
        Short   when nlr >= rolling max of nlr

    ⚠️ EXIT PROBLEM — read before trusting any S092 result.
    The book's ONLY exit for this strategy is "VWAP as dynamic stop". VWAP
    needs volume, which spot gold data lacks. With a plain static ATR stop
    the strategy has no working exit at all: the first run produced 6 trades
    in 7 years, 5 losers plus one long held 10,248 bars (2020-03 → end of
    data) that was really just buy-and-hold through gold's post-COVID rally.
    Its headline PF 15.7 / Sharpe 5.99 were artifacts of that single trade,
    NOT an edge.

    Substitute: a CHANDELIER trailing stop (highest-close-since-entry −
    mult × ATR), which preserves the "dynamic trailing stop" intent of VWAP.
    Requires cfg["trailing"] = True. Results remain a deviation from the
    book and must be labelled as such.
    """
    n_bars = cfg.get("n_bars", 5)
    power  = cfg.get("range_power", 0.5)
    atr_p  = cfg.get("atr_period", 14)
    smult  = cfg.get("stop_atr_mult", 2.0)

    acol = f"atr_{atr_p}"
    if acol not in df.columns:
        df = add_atr(df, period=atr_p)

    atr = df[acol]
    denom = atr * (n_bars ** power)

    hr = df["high"].shift(n_bars)
    lr = df["low"].shift(n_bars)

    nhr = (df["high"] - hr) / denom
    nlr = (lr - df["low"]) / denom

    max_nhr = nhr.rolling(n_bars).max().shift(1)
    max_nlr = nlr.rolling(n_bars).max().shift(1)

    long_sig  = (nhr >= max_nhr) & nhr.notna() & max_nhr.notna()
    short_sig = (nlr >= max_nlr) & nlr.notna() & max_nlr.notna()

    sig = blank_signals(df)
    entry = np.zeros(len(df), dtype=int)
    entry[long_sig.fillna(False).to_numpy()] = 1
    entry[short_sig.fillna(False).to_numpy() & ~long_sig.fillna(False).to_numpy()] = -1
    sig["entry"] = entry

    # Chandelier trailing levels — the harness ratchets these when
    # cfg["trailing"] is True, giving the strategy a real exit.
    trail_n = cfg.get("trail_bars", 22)
    sig["stop_long"]  = df["high"].rolling(trail_n, min_periods=1).max() - smult * atr
    sig["stop_short"] = df["low"].rolling(trail_n, min_periods=1).min() + smult * atr
    return _apply_er_filter(sig, df, cfg)


REGISTRY = {
    "S088_KAMA":          s088_kama,
    "S089_VIDYA":         s089_vidya,
    "S090_AdaptiveRSI":   s090_adaptive_rsi,
    "S092_AdaptiveBrkout": s092_adaptive_breakout,
}
