"""
Single-position bar simulator (numba).

Signal is known at bar i close -> enter at bar i+1 open.
Stop / target = entry -/+ k * ATR(signal bar). If a bar touches both, the STOP
is assumed first (conservative). Otherwise exit at the close of the hold-th bar.
cost = fraction per side (e.g. 0.0007 = 7 bps), charged on entry and exit.
"""
import numpy as np
import pandas as pd
from numba import njit


@njit(cache=True)
def simulate(o, h, l, c, atr, sig, hold, stop_k, tgt_k, cost):
    n = len(c)
    ent_i = np.empty(n, np.int64)
    ex_i = np.empty(n, np.int64)
    ret = np.empty(n, np.float64)
    k = 0
    i = 0
    while i < n - 2:
        s = sig[i]
        if s == 0:
            i += 1
            continue
        e = i + 1
        px = o[e]
        stop = px - s * stop_k * atr[i] if stop_k > 0 else np.nan
        tgt = px + s * tgt_k * atr[i] if tgt_k > 0 else np.nan
        last = min(e + hold - 1, n - 1)
        exit_px = c[last]
        j = e
        while j <= last:
            hit_stop = stop_k > 0 and ((s > 0 and l[j] <= stop) or (s < 0 and h[j] >= stop))
            hit_tgt = tgt_k > 0 and ((s > 0 and h[j] >= tgt) or (s < 0 and l[j] <= tgt))
            if hit_stop:
                # gap through the stop fills at the open
                exit_px = stop if not ((s > 0 and o[j] < stop) or (s < 0 and o[j] > stop)) else o[j]
                break
            if hit_tgt:
                exit_px = tgt if not ((s > 0 and o[j] > tgt) or (s < 0 and o[j] < tgt)) else o[j]
                break
            j += 1
        if j > last:
            j = last
        ent_i[k] = e
        ex_i[k] = j
        ret[k] = s * (exit_px / px - 1.0) - 2.0 * cost
        k += 1
        i = j + 1          # next signal only after the exit bar
    return ent_i[:k], ex_i[:k], ret[:k]


def stats(index: pd.DatetimeIndex, ent, ex, ret) -> dict:
    """Metrics at 1x notional, simple (non-compounded) returns."""
    n = len(ret)
    if n == 0:
        return dict(trades=0, win=np.nan, avg_bps=np.nan, total=0.0, pf=np.nan,
                    sharpe=np.nan, maxdd=0.0, cagr_simple=0.0)
    days = pd.Series(ret, index=index[ex]).groupby(index[ex].normalize()).sum()
    full = pd.date_range(index[0].normalize(), index[-1].normalize(), freq="D")
    days = days.reindex(full, fill_value=0.0)
    eq = days.cumsum()
    dd = (eq - eq.cummax()).min()
    gains, losses = ret[ret > 0].sum(), -ret[ret < 0].sum()
    years = len(full) / 365.25
    sd = days.std()
    return dict(trades=n, win=(ret > 0).mean() * 100, avg_bps=ret.mean() * 1e4,
                total=ret.sum() * 100, pf=gains / losses if losses > 0 else np.inf,
                sharpe=days.mean() / sd * np.sqrt(365) if sd > 0 else np.nan,
                maxdd=dd * 100, cagr_simple=ret.sum() / years * 100)
