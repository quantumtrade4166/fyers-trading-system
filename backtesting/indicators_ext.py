# ============================================================
# backtesting/indicators_ext.py
#
# Extended indicator library — extends backtesting/indicators.py
# (which provides EMA, RSI, VWAP, Supertrend, Bollinger).
#
# Built for the Kaufman strategy catalog backtests. Same API
# convention as indicators.py:
#   • Accepts a DataFrame with open/high/low/close (volume optional)
#   • Adds one or more deterministically-named columns
#   • Returns the same DataFrame
#
# NOTE: none of these require volume — they work on the XAUUSD
# OHLC-only dataset.
# ============================================================

import sys
import logging
from typing import Optional

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger(__name__)


# ── Core building blocks ─────────────────────────────────────

def add_sma(df: pd.DataFrame, period: int, column: str = "close",
            col_name: Optional[str] = None) -> pd.DataFrame:
    """Simple moving average. Adds sma_{period}."""
    name = col_name or f"sma_{period}"
    df[name] = df[column].rolling(period).mean()
    return df


def add_atr(df: pd.DataFrame, period: int = 14,
            col_name: Optional[str] = None) -> pd.DataFrame:
    """
    Average True Range using Wilder's smoothing (alpha = 1/period).

    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)

    Adds atr_{period}. The single most-used indicator across the
    Kaufman catalog (~40 strategies reference it).
    """
    name = col_name or f"atr_{period}"
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)

    df[name] = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    df.loc[df.index[:period], name] = np.nan
    return df


def add_roc(df: pd.DataFrame, period: int = 12, column: str = "close",
            col_name: Optional[str] = None) -> pd.DataFrame:
    """Rate of change, in percent. Adds roc_{period}."""
    name = col_name or f"roc_{period}"
    df[name] = (df[column] - df[column].shift(period)) / df[column].shift(period) * 100.0
    return df


def add_momentum(df: pd.DataFrame, period: int = 12, column: str = "close",
                 col_name: Optional[str] = None) -> pd.DataFrame:
    """Raw price momentum (Close - Close[n]). Adds mom_{period}."""
    name = col_name or f"mom_{period}"
    df[name] = df[column] - df[column].shift(period)
    return df


# ── Chande Momentum Oscillator (needed by VIDYA + S059) ──────

def add_cmo(df: pd.DataFrame, period: int = 14, column: str = "close",
            col_name: Optional[str] = None) -> pd.DataFrame:
    """
    Chande Momentum Oscillator, range -100..+100.

        CMO = 100 × (sum_up - sum_down) / (sum_up + sum_down)

    Adds cmo_{period}.
    """
    name = col_name or f"cmo_{period}"
    delta = df[column].diff()
    up   = delta.clip(lower=0).rolling(period).sum()
    down = (-delta).clip(lower=0).rolling(period).sum()

    total = up + down
    df[name] = np.where(total > 0, 100.0 * (up - down) / total, 0.0)
    df.loc[df.index[:period], name] = np.nan
    return df


# ── Efficiency Ratio (Kaufman's core concept — S091) ─────────

def add_efficiency_ratio(df: pd.DataFrame, period: int = 10, column: str = "close",
                         col_name: Optional[str] = None) -> pd.DataFrame:
    """
    Kaufman's Efficiency Ratio: net directional move / total path travelled.

        ER = |Close - Close[n]| / Σ|Close[i] - Close[i-1]|

    Range 0..1. Near 1 = clean trend; near 0 = choppy/sideways.
    This is the regime filter behind KAMA and strategy S091.

    Adds er_{period}.
    """
    name = col_name or f"er_{period}"
    direction  = (df[column] - df[column].shift(period)).abs()
    volatility = df[column].diff().abs().rolling(period).sum()

    df[name] = np.where(volatility > 0, direction / volatility, 0.0)
    df.loc[df.index[:period], name] = np.nan
    return df


# ── KAMA — Kaufman's Adaptive Moving Average (S088) ──────────

def add_kama(df: pd.DataFrame, er_period: int = 10, fast: int = 2, slow: int = 30,
             column: str = "close", col_name: Optional[str] = None) -> pd.DataFrame:
    """
    Kaufman's Adaptive Moving Average.

        fast_sc = 2/(fast+1);  slow_sc = 2/(slow+1)
        ER      = efficiency ratio over er_period
        SC      = (ER × (fast_sc - slow_sc) + slow_sc)²
        KAMA    = KAMA[1] + SC × (Close - KAMA[1])

    Adapts smoothing to trend quality: fast in clean trends, slow in chop.

    Adds kama_{er_period}_{fast}_{slow}.
    """
    name = col_name or f"kama_{er_period}_{fast}_{slow}"

    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)

    close = df[column]
    direction  = (close - close.shift(er_period)).abs()
    volatility = close.diff().abs().rolling(er_period).sum()
    er = np.where(volatility > 0, direction / volatility, 0.0)
    sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2

    close_arr = close.to_numpy(dtype=float)
    sc_arr    = np.asarray(sc, dtype=float)
    n = len(close_arr)
    kama = np.full(n, np.nan)

    start = er_period
    if n > start:
        kama[start] = close_arr[start]
        for i in range(start + 1, n):
            s = sc_arr[i]
            prev = kama[i - 1]
            if np.isnan(s) or np.isnan(prev):
                kama[i] = close_arr[i]
            else:
                kama[i] = prev + s * (close_arr[i] - prev)

    df[name] = kama
    return df


# ── VIDYA — Variable Index Dynamic Average (S089) ────────────

def add_vidya(df: pd.DataFrame, short_period: int = 9, cmo_period: int = 14,
              column: str = "close", col_name: Optional[str] = None) -> pd.DataFrame:
    """
    Chande's Variable Index Dynamic Average.

        VI    = |CMO| / 100                      (volatility index, 0..1)
        SC    = VI × 2/(short_period+1)
        VIDYA = VIDYA[1] + SC × (Close - VIDYA[1])

    Adds vidya_{short_period}_{cmo_period}.
    """
    name = col_name or f"vidya_{short_period}_{cmo_period}"

    tmp = add_cmo(df.copy(), period=cmo_period, column=column, col_name="_cmo_tmp")
    vi = (tmp["_cmo_tmp"].abs() / 100.0).to_numpy(dtype=float)
    sc_arr = vi * (2.0 / (short_period + 1))

    close_arr = df[column].to_numpy(dtype=float)
    n = len(close_arr)
    vidya = np.full(n, np.nan)

    start = cmo_period
    if n > start:
        vidya[start] = close_arr[start]
        for i in range(start + 1, n):
            s = sc_arr[i]
            prev = vidya[i - 1]
            if np.isnan(s) or np.isnan(prev):
                vidya[i] = close_arr[i]
            else:
                vidya[i] = prev + s * (close_arr[i] - prev)

    df[name] = vidya
    return df


# ── Adaptive RSI / Dynamic Momentum Index (S090) ─────────────

def add_adaptive_rsi(df: pd.DataFrame, base_period: int = 14, vol_period: int = 5,
                     vol_long: int = 10, min_period: int = 3, max_period: int = 40,
                     column: str = "close", col_name: Optional[str] = None) -> pd.DataFrame:
    """
    Chande's Dynamic Momentum Index — an RSI whose lookback shortens when
    volatility rises and lengthens when it falls.

        VI              = StdDev(Close, vol_period) / SMA(that StdDev, vol_long)
        dynamic_period  = clip(round(base_period / VI), min_period, max_period)
        DMI             = RSI(Close, dynamic_period)   [per-bar period]

    Implemented by precomputing RSI at every integer period in range and
    selecting per bar — vectorised, no Python loop over bars.

    Adds dmi_{base_period}.
    """
    name = col_name or f"dmi_{base_period}"

    close = df[column]
    cur_vol = close.rolling(vol_period).std()
    avg_vol = cur_vol.rolling(vol_long).mean()

    vi = cur_vol / avg_vol
    dyn = (base_period / vi).round()
    dyn = dyn.clip(lower=min_period, upper=max_period)

    # Precompute RSI for every candidate period, then pick per bar
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)

    rsi_by_period = {}
    for p in range(min_period, max_period + 1):
        avg_gain = gain.ewm(alpha=1.0 / p, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / p, adjust=False).mean()
        rs = avg_gain / avg_loss
        rsi_by_period[p] = 100.0 - (100.0 / (1.0 + rs))

    rsi_matrix = pd.DataFrame(rsi_by_period)              # columns = periods
    periods = dyn.fillna(base_period).astype(int).to_numpy()
    col_idx = periods - min_period
    row_idx = np.arange(len(df))

    values = rsi_matrix.to_numpy()[row_idx, col_idx]
    values[dyn.isna().to_numpy()] = np.nan

    df[name] = values
    return df


# ═══════════════════════════════════════════════════════════
# Channel & band indicators (Kaufman Batch B — S019-S028)
# ═══════════════════════════════════════════════════════════

def add_donchian(df: pd.DataFrame, period: int = 20,
                 prefix: Optional[str] = None) -> pd.DataFrame:
    """
    Donchian channel — highest high / lowest low of the last `period` bars.

    Adds dc_upper_{period}, dc_lower_{period}, dc_mid_{period}.

    NOTE: these include the CURRENT bar. Breakout strategies must compare
    against the *prior* bar's channel (shift(1)) or the test is circular —
    price can never exceed a channel it is itself setting.
    """
    p = prefix or "dc"
    up = df["high"].rolling(period).max()
    lo = df["low"].rolling(period).min()
    df[f"{p}_upper_{period}"] = up
    df[f"{p}_lower_{period}"] = lo
    df[f"{p}_mid_{period}"]   = (up + lo) / 2.0
    return df


def add_keltner(df: pd.DataFrame, ema_period: int = 20, atr_period: int = 14,
                mult: float = 2.0) -> pd.DataFrame:
    """
    Keltner channel — EMA midline with ATR-scaled bands.
    Adds kc_mid_{ema}, kc_upper_{ema}, kc_lower_{ema}.
    """
    mid = df["close"].ewm(span=ema_period, adjust=False).mean()
    acol = f"atr_{atr_period}"
    if acol not in df.columns:
        df = add_atr(df, period=atr_period)
    atr = df[acol]

    df[f"kc_mid_{ema_period}"]   = mid
    df[f"kc_upper_{ema_period}"] = mid + mult * atr
    df[f"kc_lower_{ema_period}"] = mid - mult * atr
    return df


def add_atr_bands(df: pd.DataFrame, ma_period: int = 20, atr_period: int = 14,
                  mult: float = 2.5) -> pd.DataFrame:
    """SMA midline with ATR bands. Adds atrb_mid/upper/lower_{ma_period}."""
    mid = df["close"].rolling(ma_period).mean()
    acol = f"atr_{atr_period}"
    if acol not in df.columns:
        df = add_atr(df, period=atr_period)
    atr = df[acol]

    df[f"atrb_mid_{ma_period}"]   = mid
    df[f"atrb_upper_{ma_period}"] = mid + mult * atr
    df[f"atrb_lower_{ma_period}"] = mid - mult * atr
    return df


def add_pct_bands(df: pd.DataFrame, ma_period: int = 20,
                  pct: float = 0.02) -> pd.DataFrame:
    """SMA midline with fixed-percentage bands. Adds pctb_mid/upper/lower_{ma}."""
    mid = df["close"].rolling(ma_period).mean()
    df[f"pctb_mid_{ma_period}"]   = mid
    df[f"pctb_upper_{ma_period}"] = mid * (1 + pct)
    df[f"pctb_lower_{ma_period}"] = mid * (1 - pct)
    return df


def add_linreg_channel(df: pd.DataFrame, window: int = 20, width_mult: float = 2.0,
                       column: str = "close") -> pd.DataFrame:
    """
    Rolling linear-regression channel, computed in closed form (vectorised —
    no rolling.apply, which is far too slow over 150k+ bars).

    With x fixed as 0..w-1 within each window:
        Sxx   = w(w²-1)/12                       (constant)
        Sxy   = Σ(x·y) - x̄·Σy
        slope = Sxy / Sxx
        pred  = ȳ + slope·(w-1)/2                (value at the newest bar)
        SSE   = Syy - slope²·Sxx
        stderr= sqrt(SSE/(w-2))

    Adds lr_slope_{w}, lr_mid_{w}, lr_upper_{w}, lr_lower_{w}, lr_stderr_{w}.
    All values use only bars up to and including the current one — no lookahead.
    """
    w = window
    y = df[column].astype(float)

    x_mean = (w - 1) / 2.0
    Sxx = w * (w * w - 1) / 12.0

    # Sliding dot product of y with weights 0..w-1
    weights = np.arange(w, dtype=float)
    conv = np.convolve(y.to_numpy(), weights[::-1], mode="valid")
    Sxy_raw = pd.Series(np.concatenate([np.full(w - 1, np.nan), conv]), index=y.index)

    Sy  = y.rolling(w).sum()
    Syy = (y ** 2).rolling(w).sum() - (Sy ** 2) / w
    y_mean = Sy / w

    Sxy = Sxy_raw - x_mean * Sy
    slope = Sxy / Sxx
    pred  = y_mean + slope * (w - 1 - x_mean)

    sse = (Syy - (slope ** 2) * Sxx).clip(lower=0)
    stderr = np.sqrt(sse / max(w - 2, 1))

    df[f"lr_slope_{w}"]  = slope
    df[f"lr_mid_{w}"]    = pred
    df[f"lr_stderr_{w}"] = stderr
    df[f"lr_upper_{w}"]  = pred + width_mult * stderr
    df[f"lr_lower_{w}"]  = pred - width_mult * stderr
    return df


def add_fractals(df: pd.DataFrame, n: int = 2) -> pd.DataFrame:
    """
    Bill Williams fractals — a swing high/low with `n` lower highs (or higher
    lows) on BOTH sides.

    ⚠️ A fractal is a CENTRED pattern: bar i is only confirmed as a fractal
    once n further bars have printed. The confirmed values are therefore
    shifted forward by n so a backtest can never see a fractal before it
    could actually have been known. Skipping this shift is a classic
    lookahead that makes swing systems look far better than they are.

    Adds frac_high_{n}, frac_low_{n} (price of the most recent CONFIRMED
    fractal, forward-filled).
    """
    win = 2 * n + 1
    is_high = df["high"] == df["high"].rolling(win, center=True).max()
    is_low  = df["low"]  == df["low"].rolling(win, center=True).min()

    fh = df["high"].where(is_high)
    fl = df["low"].where(is_low)

    # shift by n = the confirmation delay, then carry forward
    df[f"frac_high_{n}"] = fh.shift(n).ffill()
    df[f"frac_low_{n}"]  = fl.shift(n).ffill()
    return df


# ═══════════════════════════════════════════════════════════
# Oscillators, MA variants, SAR  (Batches A / D / E / F)
# ═══════════════════════════════════════════════════════════

def add_macd(df, fast=12, slow=26, signal=9, column="close"):
    """MACD line, signal, histogram. Adds macd, macd_signal, macd_hist."""
    f = df[column].ewm(span=fast, adjust=False).mean()
    s = df[column].ewm(span=slow, adjust=False).mean()
    df["macd"] = f - s
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    return df


def add_stochastic(df, k_period=14, k_smooth=3, d_smooth=3):
    """Stochastic %K/%D. Adds stoch_k, stoch_d."""
    ll = df["low"].rolling(k_period).min()
    hh = df["high"].rolling(k_period).max()
    rng = (hh - ll).replace(0, np.nan)
    raw = (df["close"] - ll) / rng * 100.0
    df["stoch_k"] = raw.rolling(k_smooth).mean()
    df["stoch_d"] = df["stoch_k"].rolling(d_smooth).mean()
    return df


def add_williams_r(df, period=14):
    """Williams %R, range -100..0. Adds willr_{period}."""
    hh = df["high"].rolling(period).max()
    ll = df["low"].rolling(period).min()
    rng = (hh - ll).replace(0, np.nan)
    df[f"willr_{period}"] = (hh - df["close"]) / rng * -100.0
    return df


def add_cci(df, period=20):
    """Commodity Channel Index. Adds cci_{period}."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean().replace(0, np.nan)
    df[f"cci_{period}"] = (tp - ma) / (0.015 * md)
    return df


def add_trix(df, period=15, signal=9, column="close"):
    """TRIX — 1-period % change of a triple-smoothed EMA. Adds trix, trix_signal."""
    e1 = df[column].ewm(span=period, adjust=False).mean()
    e2 = e1.ewm(span=period, adjust=False).mean()
    e3 = e2.ewm(span=period, adjust=False).mean()
    df["trix"] = 100.0 * e3.diff() / e3.shift(1)
    df["trix_signal"] = df["trix"].ewm(span=signal, adjust=False).mean()
    return df


def _wma(s: pd.Series, n: int) -> pd.Series:
    """
    Weighted moving average via convolution — vectorised.
    rolling().apply() is ~100x slower and becomes the bottleneck when
    screening dozens of strategies over million-bar 5-minute series.
    """
    w = np.arange(1, n + 1, dtype=float)
    arr = s.to_numpy(dtype=float)
    if len(arr) < n:
        return pd.Series(np.nan, index=s.index)
    conv = np.convolve(arr, w[::-1], mode="valid") / w.sum()
    return pd.Series(np.concatenate([np.full(n - 1, np.nan), conv]), index=s.index)


def add_hull(df, period=20, column="close"):
    """Hull Moving Average. Adds hma_{period}."""
    half = max(int(period // 2), 1)
    sq = max(int(np.sqrt(period)), 1)
    raw = 2 * _wma(df[column], half) - _wma(df[column], period)
    df[f"hma_{period}"] = _wma(raw, sq)
    return df


def add_psar(df, af_start=0.02, af_step=0.02, af_max=0.20):
    """
    Wilder's Parabolic SAR. Adds psar, psar_dir (+1 long / -1 short).
    Recursive by definition — implemented as an explicit loop.
    """
    high = df["high"].to_numpy(float)
    low  = df["low"].to_numpy(float)
    n = len(df)
    sar = np.full(n, np.nan)
    direction = np.zeros(n, dtype=int)

    if n < 2:
        df["psar"], df["psar_dir"] = sar, direction
        return df

    d = 1
    ep = high[0]
    af = af_start
    sar[0] = low[0]

    for i in range(1, n):
        prev = sar[i - 1]
        cur = prev + af * (ep - prev)

        if d == 1:
            cur = min(cur, low[i - 1], low[max(i - 2, 0)])
            if low[i] < cur:                      # flip to short
                d, sar[i], ep, af = -1, ep, low[i], af_start
            else:
                sar[i] = cur
                if high[i] > ep:
                    ep, af = high[i], min(af + af_step, af_max)
        else:
            cur = max(cur, high[i - 1], high[max(i - 2, 0)])
            if high[i] > cur:                     # flip to long
                d, sar[i], ep, af = 1, ep, high[i], af_start
            else:
                sar[i] = cur
                if low[i] < ep:
                    ep, af = low[i], min(af + af_step, af_max)
        direction[i] = d

    df["psar"], df["psar_dir"] = sar, direction
    return df


def add_ultimate_osc(df, p1=7, p2=14, p3=28):
    """Williams' Ultimate Oscillator. Adds uo."""
    prev_close = df["close"].shift(1)
    bp = df["close"] - pd.concat([df["low"], prev_close], axis=1).min(axis=1)
    tr = pd.concat([df["high"], prev_close], axis=1).max(axis=1) - \
         pd.concat([df["low"], prev_close], axis=1).min(axis=1)
    tr = tr.replace(0, np.nan)
    a1 = bp.rolling(p1).sum() / tr.rolling(p1).sum()
    a2 = bp.rolling(p2).sum() / tr.rolling(p2).sum()
    a3 = bp.rolling(p3).sum() / tr.rolling(p3).sum()
    df["uo"] = 100.0 * (4 * a1 + 2 * a2 + a3) / 7.0
    return df


def add_swings(df, pct: float = 0.03):
    """
    Zig-zag swing detection: a new swing is confirmed only after price
    reverses by `pct` from the running extreme.

    ⚠️ Confirmation is inherently delayed — the swing level is only KNOWN
    once the reversal happens, so the outputs are the last CONFIRMED swing
    high/low as of each bar (no lookahead).

    Adds swing_high, swing_low.
    """
    close = df["close"].to_numpy(float)
    n = len(close)
    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)

    # NOTE: direction must start at +1, never 0. With d == 0 both the
    # "d >= 0" and "d <= 0" branches fire, so `ext` just tracks price and
    # never holds a running extreme — no swing is ever confirmed.
    d = 1
    ext = close[0]
    last_h = last_l = np.nan

    for i in range(1, n):
        c = close[i]
        if d == 1:
            if c > ext:
                ext = c
            elif c < ext * (1 - pct):
                last_h, d, ext = ext, -1, c
        else:
            if c < ext:
                ext = c
            elif c > ext * (1 + pct):
                last_l, d, ext = ext, 1, c
        sh[i], sl[i] = last_h, last_l

    df["swing_high"], df["swing_low"] = sh, sl
    return df
