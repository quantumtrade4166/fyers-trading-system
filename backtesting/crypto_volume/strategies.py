"""
Volume strategy library — crypto perps, 4h / daily.

Time-series strategies: f(df, use_volume=True, **params) -> position Series in
{-1, 0, +1}, decided at the bar CLOSE (the engine fills it at the next open).
Every one takes use_volume: False runs the SAME rule with the volume part
removed (or swapped for its price-only twin), so we can see whether volume
adds anything.

Cross-sectional strategies live in the engine (they need all coins at once).
"""

import numpy as np
import pandas as pd


# ── helpers ──────────────────────────────────────────────────────────────
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _sign(s: pd.Series) -> pd.Series:
    return np.sign(s).fillna(0.0)


def _hold(long_in, long_out, short_in, short_out) -> pd.Series:
    """Stateful position from entry/exit boolean Series (entries win over exits)."""
    li, lo = long_in.fillna(False).to_numpy(), long_out.fillna(False).to_numpy()
    si, so = short_in.fillna(False).to_numpy(), short_out.fillna(False).to_numpy()
    out = np.zeros(len(li))
    p = 0.0
    for i in range(len(li)):
        if p > 0 and lo[i]:
            p = 0.0
        elif p < 0 and so[i]:
            p = 0.0
        if li[i] and not si[i]:
            p = 1.0
        elif si[i] and not li[i]:
            p = -1.0
        out[i] = p
    return pd.Series(out, index=long_in.index)


def _timed(long_trig, short_trig, bars: int) -> pd.Series:
    """Enter on a trigger, hold a fixed number of bars (a new trigger restarts)."""
    lt, st = long_trig.fillna(False).to_numpy(), short_trig.fillna(False).to_numpy()
    out = np.zeros(len(lt))
    p, left = 0.0, 0
    for i in range(len(lt)):
        if lt[i]:
            p, left = 1.0, bars
        elif st[i]:
            p, left = -1.0, bars
        out[i] = p if left > 0 else 0.0
        left -= 1
        if left <= 0:
            p = 0.0
    return pd.Series(out, index=long_trig.index)


def obv(df):
    return (np.sign(df["close"].diff()).fillna(0) * df["volume"]).cumsum()


def cvd(df):
    return (df["taker_buy_volume"] - df["taker_sell_volume"]).cumsum()


# ── time-series strategies ───────────────────────────────────────────────
def obv_trend(df, n=50, use_volume=True):
    """OBV above its EMA -> long, below -> short.  Baseline: close vs EMA."""
    x = obv(df) if use_volume else df["close"]
    return _sign(x - ema(x, n))


def cvd_trend(df, n=50, use_volume=True):
    """Cumulative taker delta (buyers - sellers) above its EMA -> long.  Baseline: close vs EMA."""
    x = cvd(df) if use_volume else df["close"]
    return _sign(x - ema(x, n))


def vwap_trend(df, n=50, use_volume=True):
    """Close vs rolling N-bar VWAP.  Baseline: close vs SMA of typical price."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    if use_volume:
        ref = (tp * df["volume"]).rolling(n, min_periods=n).sum() / df["volume"].rolling(n, min_periods=n).sum()
    else:
        ref = sma(tp, n)
    return _sign(df["close"] - ref)


def vwma_cross(df, fast=20, slow=100, use_volume=True):
    """Volume-weighted MA cross.  Baseline: plain SMA cross."""
    c, v = df["close"], df["volume"]
    if use_volume:
        f = (c * v).rolling(fast, min_periods=fast).sum() / v.rolling(fast, min_periods=fast).sum()
        s = (c * v).rolling(slow, min_periods=slow).sum() / v.rolling(slow, min_periods=slow).sum()
    else:
        f, s = sma(c, fast), sma(c, slow)
    return _sign(f - s)


def force_index(df, n=30, use_volume=True):
    """Elder force index: EMA(price change x volume).  Baseline: EMA(price change) = momentum."""
    d = df["close"].diff()
    x = d * df["volume"] if use_volume else d
    return _sign(ema(x, n))


def cmf(df, n=40, th=0.05, use_volume=True):
    """Chaikin money flow > th long, < -th short, flat in between.
    Baseline: the same close-location value, unweighted by volume."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    clv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng
    if use_volume:
        x = (clv * df["volume"]).rolling(n, min_periods=n).sum() / df["volume"].rolling(n, min_periods=n).sum()
    else:
        x = clv.rolling(n, min_periods=n).mean()
    return pd.Series(np.where(x > th, 1.0, np.where(x < -th, -1.0, 0.0)), index=df.index)


def mfi_trend(df, n=28, hi=60, use_volume=True):
    """Money flow index > hi long, < 100-hi short.  Baseline: RSI, same levels."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    if use_volume:
        mf = tp * df["volume"]
        up = mf.where(tp.diff() > 0, 0.0).rolling(n, min_periods=n).sum()
        dn = mf.where(tp.diff() < 0, 0.0).rolling(n, min_periods=n).sum()
        x = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    else:
        d = df["close"].diff()
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        x = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    return pd.Series(np.where(x > hi, 1.0, np.where(x < 100 - hi, -1.0, 0.0)), index=df.index)


def donchian_vol(df, n=40, vmult=1.5, use_volume=True):
    """N-bar breakout, entry only if volume > vmult x its N-bar average;
    exit on the opposite N/2-bar channel.  Baseline: no volume condition."""
    hh = df["high"].rolling(n, min_periods=n).max().shift()
    ll = df["low"].rolling(n, min_periods=n).min().shift()
    xh = df["high"].rolling(n // 2, min_periods=n // 2).max().shift()
    xl = df["low"].rolling(n // 2, min_periods=n // 2).min().shift()
    vol_ok = (df["volume"] > vmult * sma(df["volume"], n).shift()) if use_volume else True
    return _hold(long_in=(df["close"] > hh) & vol_ok, long_out=df["close"] < xl,
                 short_in=(df["close"] < ll) & vol_ok, short_out=df["close"] > xh)


def taker_breakout(df, n=40, ratio=0.52, use_volume=True):
    """N-bar breakout confirmed by taker aggression: buy-ratio over the
    breakout bar > ratio (longs) / < 1-ratio (shorts).  Baseline: plain breakout."""
    hh = df["high"].rolling(n, min_periods=n).max().shift()
    ll = df["low"].rolling(n, min_periods=n).min().shift()
    xh = df["high"].rolling(n // 2, min_periods=n // 2).max().shift()
    xl = df["low"].rolling(n // 2, min_periods=n // 2).min().shift()
    br = df["taker_buy_volume"] / df["volume"]
    lok = br > ratio if use_volume else True
    sok = br < 1 - ratio if use_volume else True
    return _hold(long_in=(df["close"] > hh) & lok, long_out=df["close"] < xl,
                 short_in=(df["close"] < ll) & sok, short_out=df["close"] > xh)


def spike_reversal(df, k=2.5, vmult=3.0, hold=6, use_volume=True):
    """Capitulation fade: bar return < -k sigma on volume > vmult x avg -> long
    for `hold` bars (mirror for blow-off tops).  Baseline: no volume condition."""
    r = df["close"].pct_change()
    z = r / r.rolling(90, min_periods=90).std().shift()
    vol_ok = (df["volume"] > vmult * sma(df["volume"], 30).shift()) if use_volume else True
    return _timed(long_trig=(z < -k) & vol_ok, short_trig=(z > k) & vol_ok, bars=hold)


def spike_continuation(df, k=2.0, vmult=3.0, hold=6, use_volume=True):
    """Same trigger as spike_reversal but trade WITH the move."""
    return -spike_reversal(df, k=k, vmult=vmult, hold=hold, use_volume=use_volume)


def cvd_divergence(df, n=40, hold=6, use_volume=True):
    """Price at a new N-bar high while CVD is NOT (sellers absorbing) -> short;
    mirror at lows.  Baseline (no volume): fade every new N-bar extreme."""
    c = df["close"]
    new_hi = c >= c.rolling(n, min_periods=n).max()
    new_lo = c <= c.rolling(n, min_periods=n).min()
    if use_volume:
        d = cvd(df)
        new_hi &= d < d.rolling(n, min_periods=n).max()
        new_lo &= d > d.rolling(n, min_periods=n).min()
    return _timed(long_trig=new_lo, short_trig=new_hi, bars=hold)


TS = {
    "obv_trend":         (obv_trend,        {"n": [30, 60, 120]}),
    "cvd_trend":         (cvd_trend,        {"n": [30, 60, 120]}),
    "vwap_trend":        (vwap_trend,       {"n": [30, 60, 120]}),
    "vwma_cross":        (vwma_cross,       {"fast": [10, 20], "slow": [60, 120]}),
    "force_index":       (force_index,      {"n": [20, 50, 100]}),
    "cmf":               (cmf,              {"n": [20, 40], "th": [0.0, 0.05, 0.10]}),
    "mfi_trend":         (mfi_trend,        {"n": [14, 28], "hi": [55, 60]}),
    "donchian_vol":      (donchian_vol,     {"n": [20, 40, 80], "vmult": [1.2, 1.5, 2.0]}),
    "taker_breakout":    (taker_breakout,   {"n": [20, 40, 80], "ratio": [0.51, 0.52, 0.54]}),
    "spike_reversal":    (spike_reversal,   {"k": [2.0, 3.0], "vmult": [2.0, 3.0], "hold": [3, 6, 12]}),
    "spike_continuation":(spike_continuation,{"k": [2.0, 3.0], "vmult": [2.0, 3.0], "hold": [3, 6, 12]}),
    "cvd_divergence":    (cvd_divergence,   {"n": [20, 40], "hold": [3, 6, 12]}),
}
