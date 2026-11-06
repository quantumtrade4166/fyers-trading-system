"""
Classic orderflow setups, translated to bar delta / CVD.
Each returns int8 signals: +1 long, -1 short, 0 none (known at bar close).
"""
import numpy as np
import pandas as pd


def _prior_max(s, n):
    return s.shift(1).rolling(n).max()


def _prior_min(s, n):
    return s.shift(1).rolling(n).min()


def cvd_divergence(b, n=20):
    """Price makes a new n-bar high but CVD does not -> buyers fading -> short. Mirror."""
    new_hi = b["high"] > _prior_max(b["high"], n)
    new_lo = b["low"] < _prior_min(b["low"], n)
    cvd_weak = b["cvd"] < _prior_max(b["cvd"], n)
    cvd_strong = b["cvd"] > _prior_min(b["cvd"], n)
    return np.where(new_hi & cvd_weak, -1, np.where(new_lo & cvd_strong, 1, 0)).astype(np.int8)


def absorption(b, z=2.5, k=0.15):
    """Heavy aggressive buying on a volume spike but the bar closes weak -> a passive
    seller absorbed it -> short. Mirror for heavy selling that closes strong."""
    mid = (b["high"] + b["low"]) / 2
    spike = b["vol_z"] > z
    short = spike & (b["dr"] > k) & (b["close"] < mid)
    long = spike & (b["dr"] < -k) & (b["close"] > mid)
    return np.where(long, 1, np.where(short, -1, 0)).astype(np.int8)


def imbalance_breakout(b, n=20, z=1.5, k=0.10):
    """Range breakout backed by a volume spike and one-sided delta -> follow it."""
    up = (b["close"] > _prior_max(b["high"], n)) & (b["vol_z"] > z) & (b["dr"] > k)
    dn = (b["close"] < _prior_min(b["low"], n)) & (b["vol_z"] > z) & (b["dr"] < -k)
    return np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)


def exhaustion(b, n=20, z=3.0, k=0.15):
    """Climax: new n-bar extreme on a huge volume spike with one-sided delta -> fade."""
    lo = (b["low"] < _prior_min(b["low"], n)) & (b["vol_z"] > z) & (b["dr"] < -k)
    hi = (b["high"] > _prior_max(b["high"], n)) & (b["vol_z"] > z) & (b["dr"] > k)
    return np.where(lo, 1, np.where(hi, -1, 0)).astype(np.int8)


def cvd_momentum(b, n=20, k=0.05):
    """Rolling delta ratio over n bars crosses +k / -k -> follow the flow."""
    r = b["delta"].rolling(n).sum() / b["volume"].rolling(n).sum()
    up = (r > k) & (r.shift() <= k)
    dn = (r < -k) & (r.shift() >= -k)
    return np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)


def flow_pullback(b, n=48, k=0.03):
    """Buyers in control over n bars (delta ratio > k) and price dips under EMA20
    -> buy the pullback. Mirror."""
    r = b["delta"].rolling(n).sum() / b["volume"].rolling(n).sum()
    below = (b["close"] < b["ema20"]) & (b["close"].shift() >= b["ema20"].shift())
    above = (b["close"] > b["ema20"]) & (b["close"].shift() <= b["ema20"].shift())
    return np.where((r > k) & below, 1, np.where((r < -k) & above, -1, 0)).astype(np.int8)


STRATEGIES = {
    "cvd_divergence": (cvd_divergence, [dict(n=n) for n in (10, 20, 50)]),
    "absorption": (absorption, [dict(z=z, k=k) for z in (2.0, 3.0) for k in (0.10, 0.20)]),
    "imbalance_breakout": (imbalance_breakout,
                           [dict(n=n, z=z, k=k) for n in (20, 50) for z in (1.0, 2.0) for k in (0.05, 0.15)]),
    "exhaustion": (exhaustion, [dict(n=n, z=z, k=k) for n in (20, 50) for z in (2.5, 4.0) for k in (0.10, 0.20)]),
    "cvd_momentum": (cvd_momentum, [dict(n=n, k=k) for n in (12, 48) for k in (0.03, 0.08)]),
    "flow_pullback": (flow_pullback, [dict(n=n, k=k) for n in (48, 192) for k in (0.02, 0.05)]),
}

EXITS = [dict(hold=h, stop_k=s, tgt_k=t)
         for h in (3, 6, 12, 24) for s in (1.0, 2.0) for t in (0.0, 1.5, 3.0)]
