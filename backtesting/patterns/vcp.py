import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
VCP — Volatility Contraction Pattern (Mark Minervini), as objective rules.
Not from the numbered book library (owner request, 2026-10-24).

LONG-only signal function for the shared engine (engine/harness.py);
engine/universe.combine_sides builds the mirrored short if asked.

Rules
-----
1. Swings: a causal zig-zag. A swing high is CONFIRMED once price falls
   `z` x ATR below the highest high since the last swing low (and vice
   versa), so nothing is known before it could have been seen.
2. Base: the last `n_c` contractions, each = (swing high H_k → next swing
   low L_k), depth d_k = (H_k − L_k) / H_k. Valid VCP when
     • every depth is <= `shrink` x the previous one (contracting),
     • the first depth is <= `max_first` (no crash bases),
     • the last depth is <= `max_last` (tight finish),
     • the swing lows rise (L_k > L_{k-1}),
     • no later high is more than 3% above the first high (a base, not a trend).
3. Trend template: close > SMA(50) > SMA(200) and the close is within
   `near_high` of the highest high of the last `yr` bars.
4. Volume dry-up (optional): mean volume over the last contraction <=
   `dry` x mean volume over the whole base.
5. Entry: first close above the pivot (last contraction's high H_n) with
   relative volume >= `rv_min`, within `max_wait` bars of the base completing.
6. Stop: the last contraction low L_n. Exit: close below EMA(`trail`) or the
   harness ATR/time rules set by the runner.
"""

import numpy as np
import pandas as pd

from backtesting.engine.harness import blank_signals


def _zigzag(h, l, atr, z):
    """Causal zig-zag. Returns list of confirmed swings in order:
    (kind 'H'/'L', price, bar_of_extreme, bar_confirmed)."""
    swings = []
    trend = 0                     # +1 looking for a high, -1 looking for a low
    ext_p, ext_i = h[0], 0
    lo_p, lo_i = l[0], 0
    for i in range(1, len(h)):
        a = atr[i]
        if not a == a:            # NaN ATR at the start
            continue
        if trend >= 0:
            if h[i] >= ext_p:
                ext_p, ext_i = h[i], i
            if trend == 0 and l[i] < lo_p:
                lo_p, lo_i = l[i], i
            if l[i] <= ext_p - z * a:
                swings.append(("H", ext_p, ext_i, i))
                trend, ext_p, ext_i = -1, l[i], i
                continue
            if trend == 0 and h[i] >= lo_p + z * a:
                swings.append(("L", lo_p, lo_i, i)); trend = 1
        else:
            if l[i] <= ext_p:
                ext_p, ext_i = l[i], i
            if h[i] >= ext_p + z * a:
                swings.append(("L", ext_p, ext_i, i))
                trend, ext_p, ext_i = 1, h[i], i
    return swings


def vcp(df, cfg):
    sig = blank_signals(df)
    n = len(df)
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    v = df["volume"].to_numpy(float)
    atr = df["atr"].to_numpy(float)
    rv = df["rv"].to_numpy(float)
    z, n_c = cfg.get("z", 2.0), cfg.get("n_c", 3)
    shrink, max_first, max_last = cfg.get("shrink", 0.8), cfg.get("max_first", 0.35), cfg.get("max_last", 0.10)
    dry, rv_min, max_wait = cfg.get("dry", 0.8), cfg.get("rv_min", 1.5), cfg.get("max_wait", 20)
    yr, near = cfg.get("yr", 365), cfg.get("near_high", 0.25)
    cs = pd.Series(c, index=df.index)
    sma50 = cs.rolling(cfg.get("ma_fast", 50)).mean().to_numpy()
    sma200 = cs.rolling(cfg.get("ma_slow", 200)).mean().to_numpy()
    hh = pd.Series(h).rolling(yr, min_periods=yr // 2).max().to_numpy()
    trail = cs.ewm(span=cfg.get("trail", 20), adjust=False).mean()

    swings = _zigzag(h, l, atr, z)
    # setups: when each swing LOW is confirmed, test the last n_c contractions
    setup_at = {}                  # confirm bar -> (pivot, stop)
    for k in range(len(swings)):
        kind, price, at, conf = swings[k]
        if kind != "L":
            continue
        seq = swings[max(0, k - 2 * n_c + 1):k + 1]
        if len(seq) < 2 * n_c or seq[0][0] != "H":
            continue
        H = [s for s in seq if s[0] == "H"]; L = [s for s in seq if s[0] == "L"]
        d = [(H[j][1] - L[j][1]) / H[j][1] for j in range(n_c)]
        ok = (d[0] <= max_first and d[-1] <= max_last
              and all(d[j] <= shrink * d[j - 1] for j in range(1, n_c))
              and all(L[j][1] > L[j - 1][1] for j in range(1, n_c))
              and all(H[j][1] <= H[0][1] * 1.03 for j in range(1, n_c)))
        if not ok:
            continue
        if dry:
            base = v[H[0][2]:conf + 1].mean()
            last = v[H[-1][2]:conf + 1].mean()
            if base <= 0 or last > dry * base:
                continue
        setup_at[conf] = (H[-1][1], L[-1][1])

    entry = np.zeros(n, int); stop = np.full(n, np.nan)
    pend = None                    # (pivot, stop, expiry bar)
    for i in range(n):
        if i in setup_at:
            p, s = setup_at[i]
            pend = (p, s, i + max_wait)
            continue
        if pend is None:
            continue
        p, s, exp = pend
        if i > exp or l[i] < s:
            pend = None; continue
        if (c[i] > p and rv[i] >= rv_min and c[i] > sma50[i] > sma200[i]
                and c[i] >= (1 - near) * hh[i]):
            entry[i], stop[i] = 1, s
            pend = None
    sig["entry"], sig["stop_long"] = entry, stop
    sig["exit_long"] = (df["close"] < trail).to_numpy()
    return sig
