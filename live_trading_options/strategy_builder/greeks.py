"""
strategy_builder/greeks.py — Black-76 pricing, implied vol and Greeks, vectorised.

Why Black-76 on the synthetic forward (not Black-Scholes on spot): index options
are priced off the futures/forward, and the synthetic forward (ATM strike + C - P)
already carries the carry and dividends, so no rate or dividend guess is needed.
That is also what the AlgoTest chain shows as "Synthetic FUT".

Units (match what traders read on a chain):
    iv     in %      (11.13 -> 11.13 %)
    delta  per 1 point of the underlying
    gamma  per 1 point
    theta  premium points per CALENDAR day (negative for a long option)
    vega   premium points per 1 vol point (1 %)
"""
import numpy as np
from scipy.special import ndtr

SQRT2PI = np.sqrt(2.0 * np.pi)
YEAR_DAYS = 365.0
MIN_T = 1.0 / (YEAR_DAYS * 24 * 60)        # one minute — never divide by zero at expiry


def _pdf(x):
    return np.exp(-0.5 * x * x) / SQRT2PI


def price(F, K, T, vol, is_call):
    """Black-76 premium (r = 0 — the forward already holds the carry)."""
    F, K, T, vol = (np.asarray(a, dtype=float) for a in (F, K, T, vol))
    is_call = np.asarray(is_call, dtype=bool)
    T = np.maximum(T, MIN_T)
    vol = np.maximum(vol, 1e-6)
    sd = vol * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    call = F * ndtr(d1) - K * ndtr(d2)
    put = K * ndtr(-d2) - F * ndtr(-d1)
    return np.where(is_call, call, put)


def intrinsic(F, K, is_call):
    F, K = np.asarray(F, dtype=float), np.asarray(K, dtype=float)
    return np.where(np.asarray(is_call, dtype=bool), np.maximum(F - K, 0.0), np.maximum(K - F, 0.0))


def implied_vol(prem, F, K, T, is_call, lo=0.005, hi=3.0, iters=60):
    """Vectorised bisection — slow-ish per element but unconditionally stable, and
    400 options x 60 steps is still well under a millisecond-scale budget.
    Returns vol as a FRACTION (0.1113), NaN where the premium is below intrinsic
    or not positive (no valid vol exists)."""
    prem, F, K, T = (np.asarray(a, dtype=float) for a in (prem, F, K, T))
    is_call = np.asarray(is_call, dtype=bool)
    prem, F, K, T, is_call = np.broadcast_arrays(prem, F, K, T, is_call)
    a = np.full(prem.shape, lo)
    b = np.full(prem.shape, hi)
    for _ in range(iters):
        m = 0.5 * (a + b)
        too_high = price(F, K, T, m, is_call) > prem
        b = np.where(too_high, m, b)
        a = np.where(too_high, a, m)
    iv = 0.5 * (a + b)
    bad = ~np.isfinite(prem) | (prem <= 0) | (prem < intrinsic(F, K, is_call) - 1e-9) \
        | (iv >= hi * 0.999) | (iv <= lo * 1.001)
    return np.where(bad, np.nan, iv)


def greeks(F, K, T, vol, is_call):
    """dict of delta, gamma, theta (per day), vega (per vol point). vol as a fraction."""
    F, K, T, vol = (np.asarray(a, dtype=float) for a in (F, K, T, vol))
    is_call = np.asarray(is_call, dtype=bool)
    T = np.maximum(T, MIN_T)
    vol = np.maximum(vol, 1e-6)
    sq = np.sqrt(T)
    sd = vol * sq
    d1 = (np.log(F / K) + 0.5 * sd * sd) / sd
    pdf = _pdf(d1)
    delta = np.where(is_call, ndtr(d1), ndtr(d1) - 1.0)
    gamma = pdf / (F * sd)
    theta = -(F * pdf * vol) / (2.0 * sq) / YEAR_DAYS
    vega = F * pdf * sq / 100.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}
