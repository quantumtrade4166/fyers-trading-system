import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Book #2 — Jack Schwager & Mark Etzkorn, "A Complete Guide to the Futures
Market" (2nd ed., 2017). Every mechanical rule in the book as a daily
position function:

    fn(d: dict of numpy arrays o,h,l,c (+ derived), cfg: dict) -> pos array

pos[t] is the position (+1 long, -1 short, 0 flat; pyramids may hold up to
3 units) decided at the CLOSE of day t. The runner trades it at the NEXT
open, so nothing here may look at bars after t — except where the book's
own pattern needs future bars (spike day, run day, swing points): those are
used only once those later bars have closed (confirmation delay).

Exits are close-based (the book: "the close is by far the most important
price of the day"); stops are "close beyond level", not intraday.
"""

import numpy as np

NAN = np.nan


# ── shared helpers ───────────────────────────────────────────────────────────
def prep(df) -> dict:
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    pc = np.r_[NAN, c[:-1]]
    th = np.fmax(h, pc)                      # true high / low (Ch 9)
    tl = np.fmin(l, pc)
    tr = th - tl
    d = {"o": o, "h": h, "l": l, "c": c, "th": th, "tl": tl, "tr": tr, "n": len(c),
         "dow": df.index.dayofweek.to_numpy(), "month": df.index.month.to_numpy(),
         "year": df.index.year.to_numpy(), "idx": df.index}
    d["atr20"] = _sma(tr, 20)
    d["atr10_prev"] = np.r_[NAN, _sma(tr, 10)[:-1]]          # avg TR of the PAST 10 days
    return d


def _sma(x, n):
    x = np.asarray(x, float)
    out = np.full(len(x), NAN)
    if len(x) < n:
        return out
    cs = np.cumsum(np.nan_to_num(x))
    cnt = np.cumsum(~np.isnan(x))
    s = cs[n - 1:] - np.r_[0, cs[:-n]]
    k = cnt[n - 1:] - np.r_[0, cnt[:-n]]
    out[n - 1:] = np.where(k == n, s / n, NAN)
    return out


def _lwma(x, n):
    w = np.arange(1, n + 1, dtype=float)
    out = np.full(len(x), NAN)
    for t in range(n - 1, len(x)):
        out[t] = np.dot(x[t - n + 1:t + 1], w) / w.sum()
    return out


def _ema(x, n):
    a = 2.0 / (n + 1)
    out = np.full(len(x), NAN)
    m = NAN
    for t, v in enumerate(x):
        m = v if m != m else a * v + (1 - a) * m
        out[t] = m
    return out


def _rmax(x, n):          # max of x[t-n+1 .. t]
    import pandas as pd
    return pd.Series(x).rolling(n, min_periods=n).max().to_numpy()


def _rmin(x, n):
    import pandas as pd
    return pd.Series(x).rolling(n, min_periods=n).min().to_numpy()


def _prev(x, k=1):
    return np.r_[np.full(k, NAN), x[:-k]]


def swings(h, l, k):
    """Swing high at i: h[i] > the k highs before and >= the k after.
    Returns arrays sh_i, sl_i: for each bar t, the index of the most recent
    swing high / low CONFIRMED by t (needs k bars after it), -1 if none."""
    n = len(h)
    is_h = np.zeros(n, bool); is_l = np.zeros(n, bool)
    for i in range(k, n - k):
        if h[i] > h[i - k:i].max() and h[i] >= h[i + 1:i + k + 1].max():
            is_h[i] = True
        if l[i] < l[i - k:i].min() and l[i] <= l[i + 1:i + k + 1].min():
            is_l[i] = True
    return is_h, is_l


def confirmed_lists(is_x, k):
    """For each t: list (ascending) of swing indices i with i + k <= t."""
    n = len(is_x)
    out, cur = [], []
    pts = np.where(is_x)[0]
    j = 0
    for t in range(n):
        while j < len(pts) and pts[j] + k <= t:
            cur.append(pts[j]); j += 1
        out.append(len(cur))
    return pts, np.array(out)          # pts[:out[t]] are the confirmed swings at t


def event_pos(d, long_sig, short_sig, long_stop=None, short_stop=None, hold=None,
              trail=None, target_long=None, target_short=None, reverse=True):
    """Generic one-unit trade manager on closes.
    long_sig/short_sig: bool arrays (signal at close t).
    *_stop: close-beyond-level stop set at entry. hold: max days.
    trail: lookback for a trailing close-below-N-day-low exit."""
    c = d["c"]; n = d["n"]
    lo_tr = _rmin(d["l"], trail) if trail else None
    hi_tr = _rmax(d["h"], trail) if trail else None
    pos = np.zeros(n)
    p = 0; stop = NAN; tgt = NAN; bars = 0
    for t in range(n):
        if p != 0:
            bars += 1
            ex = False
            if p > 0:
                ex = (stop == stop and c[t] < stop) or (tgt == tgt and c[t] >= tgt) or \
                     (trail and bars > 1 and c[t] < lo_tr[t - 1])
            else:
                ex = (stop == stop and c[t] > stop) or (tgt == tgt and c[t] <= tgt) or \
                     (trail and bars > 1 and c[t] > hi_tr[t - 1])
            if hold and bars >= hold:
                ex = True
            if ex:
                p = 0
        if long_sig[t] and (p < 0 and reverse or p == 0):
            p = 1; bars = 0
            stop = long_stop[t] if long_stop is not None else NAN
            tgt = target_long[t] if target_long is not None else NAN
        elif short_sig[t] and (p > 0 and reverse or p == 0):
            p = -1; bars = 0
            stop = short_stop[t] if short_stop is not None else NAN
            tgt = target_short[t] if target_short is not None else NAN
        pos[t] = p
    return pos


def _sides(pos, cfg):
    s = cfg.get("sides", "both")
    if s == "long":
        return np.where(pos > 0, pos, 0.0)
    if s == "short":
        return np.where(pos < 0, pos, 0.0)
    return pos


def _rev_from_signals(buy, sell):
    """Always-in-market: +1 after a buy signal until a sell signal."""
    pos = np.zeros(len(buy)); p = 0
    for t in range(len(buy)):
        if buy[t] and p <= 0:
            p = 1
        elif sell[t] and p >= 0:
            p = -1
        pos[t] = p
    return pos


# ── Ch 6: trend definition ───────────────────────────────────────────────────
def swing_trend(d, cfg):
    """2.1 Uptrend = higher highs & higher lows: buy on a close above the last
    confirmed swing high, sell on a close below the last swing low."""
    k = cfg.get("k", 5)
    is_h, is_l = swings(d["h"], d["l"], k)
    ph, nh = confirmed_lists(is_h, k); pl, nl = confirmed_lists(is_l, k)
    c, h, l = d["c"], d["h"], d["l"]
    buy = np.zeros(d["n"], bool); sell = buy.copy()
    for t in range(d["n"]):
        if nh[t]:
            buy[t] = c[t] > h[ph[nh[t] - 1]]
        if nl[t]:
            sell[t] = c[t] < l[pl[nl[t] - 1]]
    return _sides(_rev_from_signals(buy, sell), cfg)


def _td_lines(d, k):
    """Value at t of the current down-trend TD line (through the latest TD high
    and the latest earlier, higher TD high) and up-trend TD line."""
    h, l, n = d["h"], d["l"], d["n"]
    is_h, is_l = swings(h, l, k)
    ph, nh = confirmed_lists(is_h, k); pl, nl = confirmed_lists(is_l, k)
    dn = np.full(n, NAN); up = np.full(n, NAN)
    for t in range(n):
        if nh[t] >= 2:
            p2 = ph[nh[t] - 1]
            for j in range(nh[t] - 2, max(-1, nh[t] - 30), -1):
                p1 = ph[j]
                if h[p1] > h[p2]:
                    dn[t] = h[p2] + (h[p2] - h[p1]) / (p2 - p1) * (t - p2)
                    break
        if nl[t] >= 2:
            q2 = pl[nl[t] - 1]
            for j in range(nl[t] - 2, max(-1, nl[t] - 30), -1):
                q1 = pl[j]
                if l[q1] < l[q2]:
                    up[t] = l[q2] + (l[q2] - l[q1]) / (q2 - q1) * (t - q2)
                    break
    return dn, up


def td_line(d, cfg):
    """2.2 TD-line breakout: close above the down-trend TD line = buy, close
    below the up-trend TD line = sell (always in)."""
    dn, up = _td_lines(d, cfg.get("k", 3))
    c = d["c"]
    buy = (c > dn) & (_prev(c) <= _prev(dn))
    sell = (c < up) & (_prev(c) >= _prev(up))
    return _sides(_rev_from_signals(buy, sell), cfg)


def false_td_break(d, cfg):
    """2.25 False trend-line breakout (Ch 15): after a close above the down-
    trend line, `m` closes back below it within 10 days = sell (and vice versa).
    Stop: close beyond the post-breakout extreme; exit after `hold` days."""
    dn, up = _td_lines(d, cfg.get("k", 3))
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    m = cfg.get("m", 2)
    ls = np.zeros(n, bool); ss = ls.copy(); lst = np.full(n, NAN); sst = lst.copy()
    last_up_b = last_dn_b = -999
    cnt_u = cnt_d = 0
    for t in range(1, n):
        if c[t] > dn[t] and c[t - 1] <= dn[t - 1]:
            last_up_b, cnt_u = t, 0
        if c[t] < up[t] and c[t - 1] >= up[t - 1]:
            last_dn_b, cnt_d = t, 0
        if 0 < t - last_up_b <= 10 and c[t] < dn[t]:
            cnt_u += 1
            if cnt_u == m:
                ss[t] = True; sst[t] = h[last_up_b:t + 1].max()
        if 0 < t - last_dn_b <= 10 and c[t] > up[t]:
            cnt_d += 1
            if cnt_d == m:
                ls[t] = True; lst[t] = l[last_dn_b:t + 1].min()
    pos = event_pos(d, ls, ss, lst, sst, hold=cfg.get("hold", 20))
    return _sides(pos, cfg)


# ── Ch 7 / rules 50-51: trading-range breakout ───────────────────────────────
def _ranges(d, L, w):
    """Range of the L days BEFORE t, flagged 'consolidation' when its height
    <= w * ATR20 (as of t-1)."""
    h, l = d["h"], d["l"]
    rh = _prev(_rmax(h, L)); rl = _prev(_rmin(l, L))
    tight = (rh - rl) <= w * _prev(d["atr20"])
    return rh, rl, tight


def range_breakout(d, cfg):
    """2.4 Breakout from a trading range that held >= L days; stop on a close
    back past the range midpoint (or far side); trailing 20-day exit.
    2.21 measured-move variant: target = breakout level +/- range height x tm."""
    L, w = cfg.get("L", 15), cfg.get("w", 4.0)
    rh, rl, tight = _ranges(d, L, w)
    c = d["c"]
    buy = tight & (c > rh); sell = tight & (c < rl)
    mid = (rh + rl) / 2
    far = cfg.get("stop", "mid") == "far"
    lst = rl if far else mid; sst = rh if far else mid
    tm = cfg.get("target_mult")
    tl = rh + tm * (rh - rl) if tm else None
    ts = rl - tm * (rh - rl) if tm else None
    pos = event_pos(d, buy, sell, lst, sst, trail=cfg.get("trail", 20), target_long=tl, target_short=ts)
    return _sides(pos, cfg)


def trap(d, cfg):
    """2.24 Bull / bear trap (Ch 15): a range breakout that falls back to the
    range midpoint ('initial') or far side ('strong') within 10 days ->
    trade the other way. Stop/invalidation: close back beyond the
    post-breakout extreme. Hold up to `hold` days."""
    L, w = cfg.get("L", 15), cfg.get("w", 4.0)
    rh, rl, tight = _ranges(d, L, w)
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    strong = cfg.get("conf", "initial") == "strong"
    ls = np.zeros(n, bool); ss = ls.copy(); lst = np.full(n, NAN); sst = lst.copy()
    up = None; dn = None              # (bar, range hi, range lo)
    for t in range(n):
        if tight[t] and c[t] > rh[t]:
            up = (t, rh[t], rl[t])
        if tight[t] and c[t] < rl[t]:
            dn = (t, rh[t], rl[t])
        if up and 0 < t - up[0] <= 10:
            lvl = up[2] if strong else (up[1] + up[2]) / 2
            if c[t] < lvl:
                ss[t] = True; sst[t] = h[up[0]:t + 1].max(); up = None
        if dn and 0 < t - dn[0] <= 10:
            lvl = dn[1] if strong else (dn[1] + dn[2]) / 2
            if c[t] > lvl:
                ls[t] = True; lst[t] = l[dn[0]:t + 1].min(); dn = None
    return _sides(event_pos(d, ls, ss, lst, sst, hold=cfg.get("hold", 40)), cfg)


def narrow_top(d, cfg):
    """2.55 Rule 49: a narrow consolidation in the upper quarter of a broader
    60-day range is bullish (mirror bearish). Buy the close above the
    narrow range; stop close below it; trailing 20-day exit."""
    L, w = cfg.get("L", 10), cfg.get("w", 2.5)
    rh, rl, tight = _ranges(d, L, w)
    bh = _prev(_rmax(d["h"], 60)); bl = _prev(_rmin(d["l"], 60))
    c = d["c"]
    upper = rl >= bl + 0.75 * (bh - bl)
    lower = rh <= bl + 0.25 * (bh - bl)
    buy = tight & upper & (c > rh); sell = tight & lower & (c < rl)
    return _sides(event_pos(d, buy, sell, rl, rh, trail=20), cfg)


# ── Ch 8: support / resistance ───────────────────────────────────────────────
def envelope(d, cfg):
    """2.5 Price-envelope band (20-day MA +/- band): buy a close below the lower
    band, sell above the upper; exit at the MA; stop a further band-width
    beyond; max 20 days. Band = k x ATR20 (book uses a fixed % fitted per
    market — ATR does that fitting automatically)."""
    k = cfg.get("k", 2.5)
    ma = _sma(d["c"], 20); a = d["atr20"]; c, n = d["c"], d["n"]
    lo, hi = ma - k * a, ma + k * a
    pos = np.zeros(n); p = 0; stop = NAN; bars = 0
    for t in range(n):
        if p:
            bars += 1
            if (p > 0 and (c[t] >= ma[t] or c[t] < stop)) or (p < 0 and (c[t] <= ma[t] or c[t] > stop)) or bars >= 20:
                p = 0
        if p == 0 and c[t] < lo[t]:
            p, stop, bars = 1, lo[t] - k * a[t], 0
        elif p == 0 and c[t] > hi[t]:
            p, stop, bars = -1, hi[t] + k * a[t], 0
        pos[t] = p
    return _sides(pos, cfg)


def record_high(d, cfg):
    """2.6 Rule 48 / prior major highs: a market at a new record (all-data)
    high that HOLDS for `hold_days` closes is bought; exit on a close below
    the `x`-day low. Mirror: record lows (book warns this side is weaker)."""
    c, n = d["c"], d["n"]
    k = cfg.get("hold_days", 3); x = cfg.get("x", 50)
    rec_h = np.maximum.accumulate(np.nan_to_num(d["h"], nan=-np.inf))
    rec_l = np.minimum.accumulate(np.nan_to_num(d["l"], nan=np.inf))
    lo_x = _rmin(d["l"], x); hi_x = _rmax(d["h"], x)
    pos = np.zeros(n); p = 0
    for t in range(k + 260, n):        # need a year of history first
        old_h = rec_h[t - k]; old_l = rec_l[t - k]
        if p > 0 and c[t] < lo_x[t - 1]:
            p = 0
        if p < 0 and c[t] > hi_x[t - 1]:
            p = 0
        if p == 0 and (c[t - k + 1:t + 1] > old_h).all():
            p = 1
        elif p == 0 and (c[t - k + 1:t + 1] < old_l).all():
            p = -1
        pos[t] = p
    return _sides(pos, cfg)


# ── Ch 9: one-day patterns ───────────────────────────────────────────────────
def gap_breakaway(d, cfg):
    """2.7 Breakaway gap: an up-gap day (low > prior high) opening above the
    prior 20-day high = buy at the close; stop = close back inside the gap
    (below prior day's high); hold `hold` days with a 20-day trailing exit."""
    h, l, o, c = d["h"], d["l"], d["o"], d["c"]
    ph, pl = _prev(h), _prev(l)
    hi20 = _prev(_rmax(h, 20)); lo20 = _prev(_rmin(l, 20))
    buy = (l > ph) & (o > hi20); sell = (h < pl) & (o < lo20)
    return _sides(event_pos(d, buy, sell, ph, pl, hold=cfg.get("hold", 20), trail=20), cfg)


def island(d, cfg):
    """2.8 Island reversal: gap up, then within 10 days a gap down leaving the
    days in between entirely above (island top) -> sell; stop close above
    island high; hold `hold` days. Mirror for island bottoms."""
    h, l, n = d["h"], d["l"], d["n"]
    ls = np.zeros(n, bool); ss = ls.copy(); lst = np.full(n, NAN); sst = lst.copy()
    for t in range(2, n):
        if h[t] < l[t - 1]:                     # gap down today
            for a in range(t - 1, max(1, t - 10) - 1, -1):
                if l[a] > h[a - 1] and l[a:t].min() > h[t]:
                    ss[t] = True; sst[t] = h[a:t].max(); break
        if l[t] > h[t - 1]:                     # gap up today
            for a in range(t - 1, max(1, t - 10) - 1, -1):
                if h[a] < l[a - 1] and h[a:t].max() < l[t]:
                    ls[t] = True; lst[t] = l[a:t].min(); break
    return _sides(event_pos(d, ls, ss, lst, sst, hold=cfg.get("hold", 20)), cfg)


def _spikes(d, k=0.75, N=50):
    """Book's precise spike-high day (p.112); known one day later (needs H[t+1])."""
    h, l, c, n = d["h"], d["l"], d["c"], d["n"]
    adtr = d["atr10_prev"]
    mx = _prev(_rmax(h, N)); mn = _prev(_rmin(l, N))
    nh = np.r_[h[1:], NAN]; nl = np.r_[l[1:], NAN]
    sp_h = (h - np.fmax(_prev(h), nh) > k * adtr) & (h - c > 3 * (c - l)) & (h > mx)
    sp_l = (np.fmin(_prev(l), nl) - l > k * adtr) & (c - l > 3 * (h - c)) & (l < mn)
    sp_h[-1] = sp_l[-1] = False
    return sp_h, sp_l


def spike(d, cfg):
    """2.9 Spike reversal (rule 63): fade a spike high at the close of the day
    it is confirmed; stop = close above the spike high; hold `hold` days.
    2.11 variant `need_rev`: spike that is also a reversal day."""
    sp_h, sp_l = _spikes(d, cfg.get("k", 0.75), cfg.get("N", 50))
    c, h, l = d["c"], d["h"], d["l"]
    if cfg.get("need_rev"):
        sp_h &= c < _prev(c); sp_l &= c > _prev(c)
    ss = _prev(sp_h.astype(float)) == 1; ls = _prev(sp_l.astype(float)) == 1
    sst = _prev(h); lst = _prev(l)
    return _sides(event_pos(d, ls, ss, lst, sst, hold=cfg.get("hold", 10)), cfg)


def reversal_day(d, cfg):
    """2.10 Reversal day — the book's strict form: new 20-day high, then close
    below the low of the prior `back` day(s) -> sell; stop close above the
    reversal-day high; hold `hold` days. Mirror for reversal lows."""
    b = cfg.get("back", 1)
    h, l, c = d["h"], d["l"], d["c"]
    newh = h > _prev(_rmax(h, 20)); newl = l < _prev(_rmin(l, 20))
    ss = newh & (c < _prev(_rmin(l, b))); ls = newl & (c > _prev(_rmax(h, b)))
    return _sides(event_pos(d, ls, ss, long_stop=l, short_stop=h, hold=cfg.get("hold", 10)), cfg)


def thrust_count(d, cfg):
    """2.12 A series of up-thrust days (close > prior high) signals strength:
    >= m up-thrusts in the last w days = buy, >= m down-thrusts = sell."""
    w, m = cfg.get("w", 5), cfg.get("m", 3)
    c = d["c"]
    ut = (c > _prev(d["h"])).astype(float); dt = (c < _prev(d["l"])).astype(float)
    buy = _sma(ut, w) * w >= m; sell = _sma(dt, w) * w >= m
    return _sides(_rev_from_signals(buy, sell), cfg)


# ── Ch 9 continuation / top-bottom formations (+ Ch 15 failures) ─────────────
def _flags(d, F, w, pole):
    """Flag of F days (ending yesterday) with height <= w*ATR, preceded by a
    pole of >= pole*ATR over the 10 days before it."""
    h, l, c = d["h"], d["l"], d["c"]
    fh = _prev(_rmax(h, F)); fl = _prev(_rmin(l, F))
    a = _prev(d["atr20"])
    tight = (fh - fl) <= w * a
    c_start = _prev(c, F + 1)
    up_pole = (c_start - _prev(_rmin(l, 10), F + 1)) >= pole * a
    dn_pole = (_prev(_rmax(h, 10), F + 1) - c_start) >= pole * a
    return fh, fl, tight & up_pole, tight & dn_pole


def flag(d, cfg):
    """2.13 Flag/pennant continuation (rules 52, 57): after a pole, a tight
    flag; close beyond the flag in the pole's direction = entry; stop = close
    past the other side; 20-day trailing exit.
    2.28 mode='counter': close beyond the flag AGAINST the pole = trade that way
    (rule 58)."""
    fh, fl, upf, dnf = _flags(d, cfg.get("F", 7), cfg.get("w", 2.0), cfg.get("pole", 4.0))
    c = d["c"]
    if cfg.get("mode") == "counter":
        buy = dnf & (c > fh); sell = upf & (c < fl)
    else:
        buy = upf & (c > fh); sell = dnf & (c < fl)
    return _sides(event_pos(d, buy, sell, fl, fh, trail=20), cfg)


def flag_fail(d, cfg):
    """2.29 Opposite-direction breakout of a flag after a normal breakout:
    within 10 days of a normal flag breakout, a close beyond the far side
    of the flag reverses the trade; stop beyond the post-breakout extreme."""
    fh, fl, upf, dnf = _flags(d, cfg.get("F", 7), cfg.get("w", 2.0), cfg.get("pole", 4.0))
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    ls = np.zeros(n, bool); ss = ls.copy(); lst = np.full(n, NAN); sst = lst.copy()
    ub = db = None
    for t in range(n):
        if upf[t] and c[t] > fh[t]:
            ub = (t, fl[t])
        if dnf[t] and c[t] < fl[t]:
            db = (t, fh[t])
        if ub and 0 < t - ub[0] <= 10 and c[t] < ub[1]:
            ss[t] = True; sst[t] = h[ub[0]:t + 1].max(); ub = None
        if db and 0 < t - db[0] <= 10 and c[t] > db[1]:
            ls[t] = True; lst[t] = l[db[0]:t + 1].min(); db = None
    return _sides(event_pos(d, ls, ss, lst, sst, hold=cfg.get("hold", 40)), cfg)


def _double(d, k, tol):
    """Double tops/bottoms from swing points: two swing highs within tol*ATR,
    >= 10 bars apart; the swing low between them is the confirmation line."""
    h, l, n = d["h"], d["l"], d["n"]
    is_h, is_l = swings(h, l, k)
    ph, nh = confirmed_lists(is_h, k); pl, nl = confirmed_lists(is_l, k)
    a = d["atr20"]
    tops = []; bots = []          # (confirm-from bar, peak, line, t2)
    seen_t = set(); seen_b = set()
    for t in range(n):
        if nh[t] >= 2:
            i1, i2 = ph[nh[t] - 2], ph[nh[t] - 1]
            if (i1, i2) not in seen_t and i2 - i1 >= 10 and abs(h[i1] - h[i2]) <= tol * a[i2]:
                seen_t.add((i1, i2))
                tops.append((t, max(h[i1], h[i2]), l[i1:i2 + 1].min()))
        if nl[t] >= 2:
            j1, j2 = pl[nl[t] - 2], pl[nl[t] - 1]
            if (j1, j2) not in seen_b and j2 - j1 >= 10 and abs(l[j1] - l[j2]) <= tol * a[j2]:
                seen_b.add((j1, j2))
                bots.append((t, min(l[j1], l[j2]), h[j1:j2 + 1].max()))
    return tops, bots


def double_top(d, cfg):
    """2.14 Double top/bottom: close below the middle low (within 40 days of
    the pattern) = sell; stop close above the peaks; target = measured move
    (peak - line) below the line, or `hold` days.
    2.30 mode='fail' (Ch 15 penetration of top formation, rule 69): AFTER the
    sell confirmation, a close above the peaks = buy; stop close below line."""
    tops, bots = _double(d, cfg.get("k", 5), cfg.get("tol", 0.5))
    c, n = d["c"], d["n"]
    fail = cfg.get("mode") == "fail"
    ls = np.zeros(n, bool); ss = ls.copy()
    lst = np.full(n, NAN); sst = lst.copy(); lt = lst.copy(); st = lst.copy()
    for (t0, pk, ln) in tops:
        conf = None
        for t in range(t0, min(n, t0 + 40)):
            if c[t] < ln:
                conf = t; break
        if conf is None:
            continue
        if not fail:
            ss[conf] = True; sst[conf] = pk; st[conf] = ln - (pk - ln)
        else:
            for t in range(conf + 1, min(n, conf + 60)):
                if c[t] > pk:
                    ls[t] = True; lst[t] = ln; break
    for (t0, tr, ln) in bots:
        conf = None
        for t in range(t0, min(n, t0 + 40)):
            if c[t] > ln:
                conf = t; break
        if conf is None:
            continue
        if not fail:
            ls[conf] = True; lst[conf] = tr; lt[conf] = ln + (ln - tr)
        else:
            for t in range(conf + 1, min(n, conf + 60)):
                if c[t] < tr:
                    ss[t] = True; sst[t] = ln; break
    if fail:
        pos = event_pos(d, ls, ss, lst, sst, trail=20)
    else:
        pos = event_pos(d, ls, ss, lst, sst, hold=cfg.get("hold", 40), target_long=lt, target_short=st)
    return _sides(pos, cfg)


def head_shoulders(d, cfg):
    """2.15 Head & shoulders: three swing highs, middle highest, shoulders
    within 1.5 ATR of each other; neckline = the two troughs between them
    (sloped line); close below the neckline within 30 days = sell; stop close
    above right shoulder; target = head-to-neckline height. Mirror."""
    k = cfg.get("k", 5)
    h, l, c, n = d["h"], d["l"], d["c"], d["n"]
    a = d["atr20"]
    is_h, is_l = swings(h, l, k)
    ph, nh = confirmed_lists(is_h, k); pl, nl = confirmed_lists(is_l, k)
    ls = np.zeros(n, bool); ss = ls.copy()
    lst = np.full(n, NAN); sst = lst.copy(); lt = lst.copy(); st = lst.copy()
    done = set()
    for t in range(n):
        if nh[t] >= 3:
            s1, hd, s2 = ph[nh[t] - 3:nh[t]]
            if (s1, hd, s2) not in done and h[hd] > max(h[s1], h[s2]) and abs(h[s1] - h[s2]) <= 1.5 * a[s2]:
                done.add((s1, hd, s2))
                t1 = s1 + int(np.argmin(l[s1:hd + 1])); t2 = hd + int(np.argmin(l[hd:s2 + 1]))
                slope = (l[t2] - l[t1]) / max(1, t2 - t1)
                for u in range(t, min(n, t + 30)):
                    neck = l[t2] + slope * (u - t2)
                    if c[u] < neck:
                        ss[u] = True; sst[u] = h[s2]; st[u] = neck - (h[hd] - neck); break
                    if c[u] > h[hd]:
                        break
        if nl[t] >= 3:
            s1, hd, s2 = pl[nl[t] - 3:nl[t]]
            if ("b", s1, hd, s2) not in done and l[hd] < min(l[s1], l[s2]) and abs(l[s1] - l[s2]) <= 1.5 * a[s2]:
                done.add(("b", s1, hd, s2))
                t1 = s1 + int(np.argmax(h[s1:hd + 1])); t2 = hd + int(np.argmax(h[hd:s2 + 1]))
                slope = (h[t2] - h[t1]) / max(1, t2 - t1)
                for u in range(t, min(n, t + 30)):
                    neck = h[t2] + slope * (u - t2)
                    if c[u] > neck:
                        ls[u] = True; lst[u] = l[s2]; lt[u] = neck + (neck - l[hd]); break
                    if c[u] < l[hd]:
                        break
    pos = event_pos(d, ls, ss, lst, sst, hold=cfg.get("hold", 60), target_long=lt, target_short=st)
    return _sides(pos, cfg)


# ── Ch 11: oscillators ───────────────────────────────────────────────────────
def _rsi(c, n=14):
    dlt = np.diff(c, prepend=NAN)
    up = np.where(dlt > 0, dlt, 0.0); dn = np.where(dlt < 0, -dlt, 0.0)
    au, ad = np.full(len(c), NAN), np.full(len(c), NAN)
    if len(c) <= n:
        return au
    au[n] = up[1:n + 1].mean(); ad[n] = dn[1:n + 1].mean()
    for t in range(n + 1, len(c)):
        au[t] = (au[t - 1] * (n - 1) + up[t]) / n
        ad[t] = (ad[t - 1] * (n - 1) + dn[t]) / n
    return 100 - 100 / (1 + au / np.where(ad == 0, 1e-12, ad))


def oscillator(d, cfg):
    """2.17 Overbought/oversold fade (Ch 11 — RSI / stochastic are near-
    identical, per the book): buy oversold, exit back at the midline; max 20
    days. kind='rsi' (14, 30/70) or 'stoch' (%K 14, 20/80)."""
    c, n = d["c"], d["n"]
    if cfg.get("kind", "rsi") == "rsi":
        x = _rsi(c, 14); lo, hi = 30, 70
    else:
        hh = _rmax(d["h"], 14); ll = _rmin(d["l"], 14)
        x = 100 * (c - ll) / np.where(hh - ll == 0, NAN, hh - ll); lo, hi = 20, 80
    pos = np.zeros(n); p = 0; bars = 0
    for t in range(n):
        if p:
            bars += 1
            if (p > 0 and x[t] >= 50) or (p < 0 and x[t] <= 50) or bars >= 20:
                p = 0
        if p == 0 and x[t] < lo:
            p, bars = 1, 0
        elif p == 0 and x[t] > hi:
            p, bars = -1, 0
        pos[t] = p
    return _sides(pos, cfg)


# ── Ch 12: midtrend entry ────────────────────────────────────────────────────
def _bo_state(d, N):
    c = d["c"]
    return _rev_from_signals(c > _prev(_rmax(d["h"], N)), c < _prev(_rmin(d["l"], N)))


def retracement(d, cfg):
    """2.18 Percent retracement entry: while the N-day breakout trend is up,
    buy when price retraces r of the swing from the lowest low of the last
    N days to the highest high since; stop = close below that swing low; exit
    on an opposite breakout. Variant noWRD (rule 10): skip if the
    retracement day is a wide-ranging day."""
    N, r = cfg.get("N", 40), cfg.get("r", 0.5)
    tr_state = _bo_state(d, N)
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    wrd = d["tr"] > 2 * d["atr10_prev"]
    pos = np.zeros(n); p = 0; stop = NAN; armed = True; last_ext = NAN
    lowN = _rmin(l, N); highN = _rmax(h, N)
    for t in range(N + 1, n):
        st = tr_state[t]
        if p > 0 and (st < 0 or c[t] < stop):
            p = 0
        if p < 0 and (st > 0 or c[t] > stop):
            p = 0
        if st > 0:
            hi = h[t - N + 1:t + 1].max(); lo = lowN[t]
            if last_ext != hi:
                armed, last_ext = True, hi
            if p == 0 and armed and c[t] <= hi - r * (hi - lo) and not (cfg.get("noWRD") and wrd[t]):
                p, stop, armed = 1, lo, False
        elif st < 0:
            lo = l[t - N + 1:t + 1].min(); hi = highN[t]
            if last_ext != lo:
                armed, last_ext = True, lo
            if p == 0 and armed and c[t] >= lo + r * (hi - lo) and not (cfg.get("noWRD") and wrd[t]):
                p, stop, armed = -1, hi, False
        pos[t] = p
    return _sides(pos, cfg)


def minor_reaction(d, cfg):
    """2.19 Reversal of minor reaction (book's exact counts): in an uptrend a
    'reaction' is defined when the reaction count reaches 4 (days with high
    AND low <= those of the count's reference day); the trend resumes when 3
    up-thrust days follow (count resets if the reaction low breaks). Stop =
    close below the reaction low. Trend = N-day breakout state."""
    N = cfg.get("N", 40)
    st = _bo_state(d, N)
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    pos = np.zeros(n); p = 0; stop = NAN
    ref = None; rc = 0; reac = False; tc = 0; rlow = NAN; ext = NAN
    for t in range(1, n):
        if p > 0 and (st[t] < 0 or c[t] < stop):
            p = 0
        if p < 0 and (st[t] > 0 or c[t] > stop):
            p = 0
        s = st[t]
        if s != st[t - 1]:
            ref = None; rc = 0; reac = False; tc = 0; ext = NAN
        if s > 0:
            if ext != ext or h[t] > ext:
                ext = h[t]; ref = t; rc = 0; reac = False; tc = 0
            elif not reac and h[t] <= h[ref] and l[t] <= l[ref]:
                rc += 1; ref = t
                if rc >= 4:
                    reac = True; rlow = l[t]; tc = 0
            if reac:
                if l[t] < rlow:                    # reaction low penetrated
                    tc = 0; rlow = l[t]
                if c[t] > h[t - 1]:
                    tc += 1
                if tc >= 3 and p == 0:
                    p, stop = 1, rlow; reac = False; rc = 0
        elif s < 0:
            if ext != ext or l[t] < ext:
                ext = l[t]; ref = t; rc = 0; reac = False; tc = 0
            elif not reac and h[t] >= h[ref] and l[t] >= l[ref]:
                rc += 1; ref = t
                if rc >= 4:
                    reac = True; rlow = h[t]; tc = 0
            if reac:
                if h[t] > rlow:
                    tc = 0; rlow = h[t]
                if c[t] < l[t - 1]:
                    tc += 1
                if tc >= 3 and p == 0:
                    p, stop = -1, rlow; reac = False; rc = 0
        pos[t] = p
    return _sides(pos, cfg)


def ma_reaction(d, cfg):
    """2.20 Reaction to the long-term MA: while the 40-day MA is rising (vs 20
    days ago), buy a close below it; exit when the MA stops rising or after
    `hold` days; stop = close more than 2 ATR below entry. Mirror."""
    N = cfg.get("N", 40)
    c, n = d["c"], d["n"]
    ma = _sma(c, N); rising = ma > _prev(ma, 20); falling = ma < _prev(ma, 20)
    a = d["atr20"]; hold = cfg.get("hold", 20)
    pos = np.zeros(n); p = 0; stop = NAN; bars = 0
    for t in range(n):
        if p:
            bars += 1
            if (p > 0 and (not rising[t] or c[t] < stop)) or (p < 0 and (not falling[t] or c[t] > stop)) or bars >= hold:
                p = 0
        if p == 0 and rising[t] and c[t] < ma[t]:
            p, stop, bars = 1, c[t] - 2 * a[t], 0
        elif p == 0 and falling[t] and c[t] > ma[t]:
            p, stop, bars = -1, c[t] + 2 * a[t], 0
        pos[t] = p
    return _sides(pos, cfg)


# ── Ch 14: DeMark sequential ─────────────────────────────────────────────────
def sequential(d, cfg):
    """2.22 TD Sequential exactly as the book summarises it: setup (9 closes
    vs close 4 days earlier), intersection, countdown to 13 (close vs low/high
    2 days earlier), with the three cancellation rules. Buy-13 = long,
    sell-13 = short; hold = 'rev' (until opposite 13) or N days."""
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    buy = np.zeros(n, bool); sell = buy.copy()
    bs = ss = 0                                   # consecutive setup counts
    b_active = s_active = False; b_cd = s_cd = 0
    b_inter = s_inter = False; b_hi = s_lo = NAN; b_start = s_start = 0
    for t in range(4, n):
        bs = bs + 1 if c[t] < c[t - 4] else 0
        ss = ss + 1 if c[t] > c[t - 4] else 0
        if bs == 9:                               # new buy setup (supersedes)
            b_active, b_cd, b_inter = True, 0, False
            b_start = t - 8; b_hi = h[b_start:t + 1].max()
            s_active = False                      # (b) opposite setup cancels
        if ss == 9:
            s_active, s_cd, s_inter = True, 0, False
            s_start = t - 8; s_lo = l[s_start:t + 1].min()
            b_active = False
        if b_active:
            if c[t] > b_hi:
                b_active = False
            else:
                # intersection: a high on/after setup day 8 above the low of a
                # day 3+ days earlier (within the setup)
                if not b_inter and t >= b_start + 7 and t - 2 > b_start:
                    b_inter = h[t] > l[b_start:t - 2].min()
                if b_inter and c[t] <= l[t - 2]:
                    b_cd += 1
                    if b_cd >= 13:
                        buy[t] = True; b_active = False
        if s_active:
            if c[t] < s_lo:
                s_active = False
            else:
                if not s_inter and t >= s_start + 7 and t - 2 > s_start:
                    s_inter = l[t] < h[s_start:t - 2].max()
                if s_inter and c[t] >= h[t - 2]:
                    s_cd += 1
                    if s_cd >= 13:
                        sell[t] = True; s_active = False
    hold = cfg.get("hold", "rev")
    if hold == "rev":
        pos = _rev_from_signals(buy, sell)
    else:
        pos = event_pos(d, buy, sell, hold=hold)
    return _sides(pos, cfg)


# ── Ch 15: other failed signals ──────────────────────────────────────────────
def spike_return(d, cfg):
    """2.26 Return to spike extreme: within 60 days of a spike HIGH, a close
    above the spike high = buy (the failed top); stop = close below the
    spike day's low; 20-day trailing exit. Mirror."""
    sp_h, sp_l = _spikes(d)
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    ls = np.zeros(n, bool); ss = ls.copy(); lst = np.full(n, NAN); sst = lst.copy()
    for i in np.where(sp_h)[0]:
        for t in range(i + 2, min(n, i + 61)):
            if c[t] > h[i]:
                ls[t] = True; lst[t] = l[i]; break
    for i in np.where(sp_l)[0]:
        for t in range(i + 2, min(n, i + 61)):
            if c[t] < l[i]:
                ss[t] = True; sst[t] = h[i]; break
    return _sides(event_pos(d, ls, ss, lst, sst, trail=20), cfg)


def wrd_return(d, cfg):
    """2.27 Return to wide-ranging-day extreme: a WRD (TR > 2x avg TR of the
    past 10 days) that closed in its lower half, later (<= 60 days) closed
    above its true high = buy; stop close below WRD midpoint; 20-day
    trailing exit. Mirror."""
    th, tl, c, o, n = d["th"], d["tl"], d["c"], d["o"], d["n"]
    wrd = d["tr"] > cfg.get("k", 2.0) * d["atr10_prev"]
    mid = (th + tl) / 2
    ls = np.zeros(n, bool); ss = ls.copy(); lst = np.full(n, NAN); sst = lst.copy()
    for i in np.where(wrd)[0]:
        if c[i] < mid[i]:
            for t in range(i + 1, min(n, i + 61)):
                if c[t] > th[i]:
                    ls[t] = True; lst[t] = mid[i]; break
        elif c[i] > mid[i]:
            for t in range(i + 1, min(n, i + 61)):
                if c[t] < tl[i]:
                    ss[t] = True; sst[t] = mid[i]; break
    return _sides(event_pos(d, ls, ss, lst, sst, trail=20), cfg)


# ── Ch 16: trend-following systems ───────────────────────────────────────────
def _ma(c, n, kind):
    return {"sma": _sma, "lwma": _lwma, "ema": _ema}[kind](c, n)


def price_ma(d, cfg):
    """2.32 Close crosses its N-day moving average (SMA / LWMA / EWMA).
    2.38 filter=True: accept only when the MA is moving the same way
    (rejected signals activate later if the MA turns)."""
    c = d["c"]; ma = _ma(c, cfg.get("N", 35), cfg.get("ma", "sma"))
    if cfg.get("filter"):
        buy = (c > ma) & (ma > _prev(ma)); sell = (c < ma) & (ma < _prev(ma))
    else:
        buy = (c > ma) & (_prev(c) <= _prev(ma)); sell = (c < ma) & (_prev(c) >= _prev(ma))
    return _sides(_rev_from_signals(buy, sell), cfg)


def dual_ma(d, cfg):
    """2.33 Crossover of a short and a long simple MA (e.g. 12/48)."""
    c = d["c"]; f = _sma(c, cfg.get("f", 12)); s = _sma(c, cfg.get("s", 48))
    return _sides(_rev_from_signals(f > s, f < s), cfg)


def breakout(d, cfg):
    """2.34 Close above the prior N-day high = long, below the N-day low =
    short (always in). 2.40 asymmetric: separate sell length Ns.
    2.39 volmap: N picked from 5 volatility classes (avg 2-day range over 50
    days, ranked vs the past 500 days)."""
    c, h, l = d["c"], d["h"], d["l"]
    if cfg.get("volmap"):
        r2 = _rmax(h, 2) - _rmin(l, 2)
        v = _sma(r2, 50) / c
        import pandas as pd
        rk = pd.Series(v).rolling(500, min_periods=250).rank(pct=True).to_numpy()
        cls = np.clip(np.nan_to_num(rk, nan=0.5) * 5, 0, 4.999).astype(int)
        Ns = [20, 30, 40, 50, 60]
        if cfg["volmap"] == "inverse":
            Ns = Ns[::-1]
        hi = np.vstack([_prev(_rmax(h, k)) for k in Ns]); lo = np.vstack([_prev(_rmin(l, k)) for k in Ns])
        idx = np.arange(len(c))
        buy = c > hi[cls, idx]; sell = c < lo[cls, idx]
    else:
        N = cfg.get("N", 40); Nsell = cfg.get("Ns", N)
        buy = c > _prev(_rmax(h, N)); sell = c < _prev(_rmin(l, Nsell))
    return _sides(_rev_from_signals(buy, sell), cfg)


def breakout_confirm(d, cfg):
    """2.35-2.37 Breakout with a confirmation condition: the base N-day signal
    is only taken if, before an opposite base signal,
      pen  : a close beyond the signal price by p x ATR  (book: 3%)
      delay: a close beyond the signal price >= 6 days after the signal
      thrust: 3 thrust days beyond the signal price."""
    N = cfg.get("N", 12); kind = cfg.get("conf", "pen")
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    bb = c > _prev(_rmax(h, N)); bs = c < _prev(_rmin(l, N))
    a = d["atr20"]
    pos = np.zeros(n); p = 0; pend = 0; sp = NAN; s_t = 0; cnt = 0; pa = NAN
    for t in range(1, n):
        # an opposite base signal cancels an unconfirmed one
        if (bs[t] and pend == 1) or (bb[t] and pend == -1):
            pend = 0
        if bb[t] and pend != 1 and p <= 0:
            pend, sp, s_t, cnt, pa = 1, c[t], t, 0, a[t]
        elif bs[t] and pend != -1 and p >= 0:
            pend, sp, s_t, cnt, pa = -1, c[t], t, 0, a[t]
        if pend:
            ok = False
            if kind == "pen":
                ok = c[t] > sp + cfg.get("p", 1.0) * pa if pend > 0 else c[t] < sp - cfg.get("p", 1.0) * pa
            elif kind == "delay":
                ok = t - s_t >= cfg.get("days", 6) and (c[t] > sp if pend > 0 else c[t] < sp)
            else:
                if t > s_t and (c[t] > h[t - 1] and c[t] > sp if pend > 0 else c[t] < l[t - 1] and c[t] < sp):
                    cnt += 1
                ok = cnt >= 3
            if ok:
                p, pend = pend, 0
        pos[t] = p
    return _sides(pos, cfg)


def pyramid(d, cfg):
    """2.41 Breakout + the book's pyramid plan: a reaction = close below the
    prior 10-day low while long; afterwards add a unit on a new 10-day high
    if above the last entry price and units < 3. Pyramid units exit on an
    opposite signal or a close below the low since the latest reaction."""
    N = cfg.get("N", 40)
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    st = _bo_state(d, N)
    h10 = _prev(_rmax(h, 10)); l10 = _prev(_rmin(l, 10))
    pos = np.zeros(n); units = 0; reac = False; last_px = NAN; rlow = NAN; pstop = NAN
    for t in range(1, n):
        s = st[t]
        if s != st[t - 1]:
            units = int(s); reac = False; last_px = c[t]; rlow = pstop = NAN
        elif s > 0:
            if units > 1 and c[t] < pstop:          # pyramid stop
                units = 1; pstop = NAN
            if not reac and c[t] < l10[t]:
                reac = True; rlow = l[t]
            elif reac:
                rlow = min(rlow, l[t])
                if c[t] > h10[t] and c[t] > last_px and units < 3:
                    units += 1; last_px = c[t]; reac = False; pstop = rlow
        elif s < 0:
            if units < -1 and c[t] > pstop:
                units = -1; pstop = NAN
            if not reac and c[t] > h10[t]:
                reac = True; rlow = h[t]
            elif reac:
                rlow = max(rlow, h[t])
                if c[t] < l10[t] and c[t] < last_px and units > -3:
                    units -= 1; last_px = c[t]; reac = False; pstop = rlow
        pos[t] = units
    return _sides(pos, cfg)


def breakout_exit(d, cfg):
    """2.42 Breakout + trade-exit rule: exit a long on a close below the M-day
    low (M < N), re-enter only on a fresh N-day high. as_reversal=True: the
    exit reverses the position instead (the book says this helps more)."""
    N = cfg.get("N", 40); M = cfg.get("M", 20)
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    bN, sN = c > _prev(_rmax(h, N)), c < _prev(_rmin(l, N))
    xL, xS = c < _prev(_rmin(l, M)), c > _prev(_rmax(h, M))
    rev = cfg.get("as_reversal", False)
    pos = np.zeros(n); p = 0
    for t in range(n):
        if p > 0 and xL[t]:
            p = -1 if rev else 0
        elif p < 0 and xS[t]:
            p = 1 if rev else 0
        if bN[t] and p <= 0:
            p = 1
        elif sN[t] and p >= 0:
            p = -1
        pos[t] = p
    return _sides(pos, cfg)


def exit_pack(d, cfg):
    """2.58 Rules 42/16/18 bolted onto the N-day breakout:
      friday : don't carry a losing trade over the weekend (flat until the
               next fresh signal)
      day1   : dramatically wrong on day 1 (loss > 1 ATR) -> out
      vol    : a day moving against the position by > 2 ATR -> out."""
    N = cfg.get("N", 40); rule = cfg.get("rule", "friday")
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    bN, sN = c > _prev(_rmax(h, N)), c < _prev(_rmin(l, N))
    a = d["atr20"]; dow = d["dow"]
    pos = np.zeros(n); p = 0; ent = NAN; bars = 0
    for t in range(1, n):
        if p:
            bars += 1
            pnl = (c[t] - ent) * p
            if rule == "friday" and dow[t] == 4 and pnl < 0:
                p = 0
            elif rule == "day1" and bars == 1 and pnl < -a[t]:
                p = 0
            elif rule == "vol" and (c[t] - c[t - 1]) * p < -2 * a[t - 1]:
                p = 0
        if bN[t] and p <= 0 and not (p == 0 and bN[t - 1]):
            p, ent, bars = 1, c[t], 0
        elif sN[t] and p >= 0 and not (p == 0 and sN[t - 1]):
            p, ent, bars = -1, c[t], 0
        pos[t] = p
    return _sides(pos, cfg)


def fade_move(d, cfg):
    """2.43 Fading a minimum move (counter-trend, always in): sell once the
    market rallies X above the low since the last buy; buy once it falls X
    below the high since the last sell. X = k x ATR20 (book: 7.5%).
    2.44 thrust=True: wait for one thrust day the new way before entering.
    stop=True: drop to flat if the trade goes X against us."""
    k = cfg.get("k", 6.0)
    c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    a = d["atr20"]
    # lo = lowest low since the last buy (or reset), hi = highest high since
    # the last sell (or reset)
    pos = np.zeros(n); p = 0; lo = hi = NAN; pend = 0; ent = NAN
    for t in range(21, n):
        X = k * a[t]
        lo = l[t] if lo != lo else min(lo, l[t])
        hi = h[t] if hi != hi else max(hi, h[t])
        if cfg.get("stop") and p and (c[t] - ent) * p < -X:
            p = 0; lo, hi = l[t], h[t]; pend = 0
        want_sell = p != -1 and c[t] >= lo + X
        want_buy = p != 1 and c[t] <= hi - X
        if want_sell and not want_buy:
            pend = -1
        elif want_buy and not want_sell:
            pend = 1
        if pend:
            ok = not cfg.get("thrust") or (c[t] < l[t - 1] if pend < 0 else c[t] > h[t - 1])
            if ok:
                p = pend; pend = 0; ent = c[t]
                lo, hi = l[t], h[t]
        pos[t] = p
    return _sides(pos, cfg)


# ── Ch 17: original systems ──────────────────────────────────────────────────
def wrd_system(d, cfg):
    """2.46 Wide-ranging-day system (book's exact daily checklist): VR = TR /
    avg TR of the past 10 days; WRD when VR > k. PTR = highest true high /
    lowest true low from N1 days before to N2 days after the latest WRD,
    defined N2 days after it. Signals are checked BEFORE the PTR is reset."""
    k, N1, N2 = cfg.get("k", 2.0), cfg.get("N1", 0), cfg.get("N2", 0)
    th, tl, c, n = d["th"], d["tl"], d["c"], d["n"]
    wrd = d["tr"] > k * d["atr10_prev"]
    pos = np.zeros(n); p = 0; ptr_h = ptr_l = NAN; last_w = -1
    for t in range(n):
        if ptr_h == ptr_h:
            if p <= 0 and c[t] > ptr_h:
                p = 1
            elif p >= 0 and c[t] < ptr_l:
                p = -1
        if wrd[t]:
            last_w = t
        if last_w >= 0 and t - last_w == N2 and last_w - N1 >= 0:
            ptr_h = th[last_w - N1:t + 1].max(); ptr_l = tl[last_w - N1:t + 1].min()
        pos[t] = p
    return _sides(pos, cfg)


def _run_days(d, N1):
    """Up run day: true high > max true high of the prior N1 days AND true low
    < min true low of the NEXT N1 days (known N1 days later). Days meeting
    both definitions are not run days."""
    th, tl, n = d["th"], d["tl"], d["n"]
    up = np.zeros(n, bool); dn = up.copy()
    for i in range(N1, n - N1):
        u = th[i] > th[i - N1:i].max() and tl[i] < tl[i + 1:i + N1 + 1].min()
        w = tl[i] < tl[i - N1:i].min() and th[i] > th[i + 1:i + N1 + 1].max()
        up[i], dn[i] = u and not w, w and not u
    return up, dn


def run_breakout(d, cfg):
    """2.47 Run-day breakout: reverse to long on a close above the max true
    high of the last N2 down run days if the latest run day is UP (mirror for
    sells). Run days are only used N1 days after they occur."""
    N1, N2 = cfg.get("N1", 5), cfg.get("N2", 4)
    up, dn = _run_days(d, N1)
    th, tl, c, n = d["th"], d["tl"], d["c"], d["n"]
    pos = np.zeros(n); p = 0
    ups, dns, last = [], [], 0
    for t in range(n):
        i = t - N1
        if i >= 0:
            if up[i]:
                ups.append(i); last = 1
            if dn[i]:
                dns.append(i); last = -1
        if p <= 0 and len(dns) >= N2 and last == 1 and c[t] > max(th[j] for j in dns[-N2:]):
            p = 1
        elif p >= 0 and len(ups) >= N2 and last == -1 and c[t] < min(tl[j] for j in ups[-N2:]):
            p = -1
        pos[t] = p
    return _sides(pos, cfg)


def run_count(d, cfg):
    """2.48 Run-day consecutive count: reverse when N2 run days in one
    direction occur with no opposite run day in between (known N1 days
    after the last one)."""
    N1, N2 = cfg.get("N1", 5), cfg.get("N2", 3)
    up, dn = _run_days(d, N1)
    n = d["n"]
    pos = np.zeros(n); p = 0; bc = sc = 0
    for t in range(n):
        i = t - N1
        if i >= 0:
            if up[i]:
                if p <= 0:
                    bc += 1
                sc = 0
            if dn[i]:
                if p >= 0:
                    sc += 1
                bc = 0
        if p <= 0 and bc >= N2:
            p, bc, sc = 1, 0, 0
        elif p >= 0 and sc >= N2:
            p, bc, sc = -1, 0, 0
        pos[t] = p
    return _sides(pos, cfg)


# ── Ch 26-27: seasonal, event response ───────────────────────────────────────
def seasonal(d, cfg):
    """2.50 Seasonal index (link-relative style, Ch 26), built ONLY from
    earlier years (>= 5): each month, hold the sign of that calendar month's
    average past return minus the average of all months ('detrend').
    mode='extremes': only the best 3 (long) / worst 3 (short) months."""
    import pandas as pd
    c = d["c"]; idx = d["idx"]
    s = pd.Series(c, index=idx)
    mret = s.resample("ME").last().pct_change()
    pos_m = {}
    for i, (ts, _) in enumerate(mret.items()):
        hist = mret.iloc[:i].dropna()
        if hist.index.year.nunique() < 5:
            continue
        g = hist.groupby(hist.index.month).mean()
        if ts.month not in g:
            continue
        rel = g - g.mean()
        if cfg.get("mode") == "extremes":
            top = set(rel.nlargest(3).index); bot = set(rel.nsmallest(3).index)
            pos_m[(ts.year, ts.month)] = 1 if ts.month in top else -1 if ts.month in bot else 0
        else:
            pos_m[(ts.year, ts.month)] = float(np.sign(rel[ts.month]))
    # position decided at the prior month's last close -> held through month
    n = d["n"]; pos = np.zeros(n)
    yr, mo = d["year"], d["month"]
    for t in range(n - 1):
        nxt = (yr[t + 1], mo[t + 1])
        pos[t] = pos_m.get(nxt, 0.0)
    pos[-1] = pos[-2] if n > 1 else 0
    return _sides(pos, cfg)


def jobs_report(d, cfg):
    """2.51 Market response to the monthly US jobs report (first Friday):
    response = close in the top / bottom 25% of the day's range (book's
    stock-index test). Trade from that close for `hold` days, either
    'follow' the response or 'fade' it (rule 70)."""
    idx = d["idx"]; c, h, l, n = d["c"], d["h"], d["l"], d["n"]
    first_fri = (idx.dayofweek == 4) & (idx.day <= 7)
    pr = (c - l) / np.where(h - l == 0, NAN, h - l)
    bull = first_fri & (pr >= 0.75); bear = first_fri & (pr <= 0.25)
    if cfg.get("mode", "follow") == "fade":
        bull, bear = bear, bull
    return _sides(event_pos(d, bull, bear, hold=cfg.get("hold", 5)), cfg)


# ── Ch 37 rules ──────────────────────────────────────────────────────────────
def vertical(d, cfg):
    """2.56 Rule 62: a near-vertical move over 3 days coming off a 20-day low
    (move >= k x ATR) tends to extend: buy, hold `hold` days, stop = close
    below the move's start low. Mirror."""
    k = cfg.get("k", 4.0)
    c, h, l = d["c"], d["h"], d["l"]
    a = _prev(d["atr20"], 3)
    lo3 = _prev(_rmin(l, 3)); hi3 = _prev(_rmax(h, 3))
    lo20 = _prev(_rmin(l, 20), 1); hi20 = _prev(_rmax(h, 20), 1)
    from_low = lo3 <= _prev(_rmin(l, 20)) + 1e-12
    from_high = hi3 >= _prev(_rmax(h, 20)) - 1e-12
    buy = from_low & (c - lo3 >= k * a); sell = from_high & (hi3 - c >= k * a)
    return _sides(event_pos(d, buy, sell, lo3, hi3, hold=cfg.get("hold", 10)), cfg)


# registry: id -> (name, fn, list of cfgs)
def _g(**kw):
    import itertools
    keys = list(kw)
    return [dict(zip(keys, v)) for v in itertools.product(*kw.values())]


WRD_SETS = [{"k": k, "N1": a, "N2": b} for k in (1.6, 2.0, 2.4) for b in (0, 2, 4) for a in (0, 2, 4)]
RUN_BO_SETS = [{"N1": a, "N2": b} for a, b in ((3, 2), (3, 3), (3, 4), (3, 5), (5, 2), (5, 3), (5, 4), (7, 2), (7, 3), (7, 4))]
RUN_CT_SETS = [{"N1": a, "N2": b} for a, b in ((3, 1), (3, 2), (3, 3), (3, 4), (5, 1), (5, 2), (5, 3), (7, 1), (7, 2), (7, 3))]

BOOK = [
    ("2.1", "Swing high/low trend", swing_trend, _g(k=[3, 5, 10])),
    ("2.2", "TD-line breakout", td_line, _g(k=[2, 3, 5])),
    ("2.4", "Trading-range breakout", range_breakout, _g(L=[10, 15, 20], w=[3.0, 4.0, 5.0], stop=["mid", "far"])),
    ("2.5", "Price envelope band fade", envelope, _g(k=[2.0, 2.5, 3.0])),
    ("2.6", "Record high & hold", record_high, _g(hold_days=[1, 3, 5], x=[50, 100])),
    ("2.7", "Breakaway gap", gap_breakaway, _g(hold=[10, 20, 40])),
    ("2.8", "Island reversal", island, _g(hold=[10, 20])),
    ("2.9", "Spike day reversal", spike, _g(k=[0.5, 0.75], N=[20, 50], hold=[5, 10, 20])),
    ("2.10", "Reversal day (strict)", reversal_day, _g(back=[1, 2], hold=[5, 10, 20])),
    ("2.11", "Spike + reversal day", spike, _g(k=[0.5, 0.75], N=[20, 50], hold=[5, 10, 20], need_rev=[True])),
    ("2.12", "Thrust-day count", thrust_count, _g(w=[5, 8], m=[3, 4])),
    ("2.13", "Flag/pennant continuation", flag, _g(F=[5, 7, 10], w=[1.5, 2.5], pole=[3.0, 5.0])),
    ("2.14", "Double top/bottom", double_top, _g(k=[3, 5], tol=[0.5, 1.0], hold=[20, 40])),
    ("2.15", "Head & shoulders", head_shoulders, _g(k=[3, 5], hold=[30, 60])),
    ("2.17", "Overbought/oversold fade", oscillator, _g(kind=["rsi", "stoch"])),
    ("2.18", "Percent-retracement entry", retracement, _g(N=[20, 40], r=[0.35, 0.5, 0.65], noWRD=[False, True])),
    ("2.19", "Reversal of minor reaction", minor_reaction, _g(N=[20, 40, 60])),
    ("2.20", "Reaction to 40-day MA", ma_reaction, _g(N=[40], hold=[10, 20, 40])),
    ("2.21", "Range breakout + measured-move target", range_breakout, _g(L=[10, 15, 20], w=[4.0], target_mult=[1.0, 2.0])),
    ("2.22", "DeMark Sequential", sequential, _g(hold=["rev", 10, 20])),
    ("2.24", "Bull/bear trap", trap, _g(L=[10, 15, 20], w=[4.0], conf=["initial", "strong"])),
    ("2.25", "False trend-line breakout", false_td_break, _g(k=[3, 5], m=[1, 2], hold=[10, 20])),
    ("2.26", "Return to spike extreme", spike_return, [{}]),
    ("2.27", "Return to wide-ranging-day extreme", wrd_return, _g(k=[1.6, 2.0, 2.4])),
    ("2.28", "Counter-to-anticipated flag breakout", flag, _g(F=[5, 7, 10], w=[1.5, 2.5], pole=[3.0, 5.0], mode=["counter"])),
    ("2.29", "Opposite flag breakout after normal", flag_fail, _g(F=[5, 7, 10], w=[1.5, 2.5], pole=[3.0, 5.0])),
    ("2.30", "Penetration of top/bottom formation", double_top, _g(k=[3, 5], tol=[0.5, 1.0], mode=["fail"])),
    ("2.32", "Price/MA crossover", price_ma, _g(N=[10, 20, 35, 50, 100], ma=["sma", "lwma", "ema"])),
    ("2.33", "Dual MA crossover", dual_ma, [{"f": f, "s": s} for f, s in ((5, 20), (10, 40), (12, 48), (20, 100), (50, 200))]),
    ("2.34", "N-day close breakout", breakout, _g(N=[7, 10, 20, 40, 60, 100])),
    ("2.35", "Breakout + penetration confirm", breakout_confirm, _g(N=[12, 20, 40], conf=["pen"], p=[0.5, 1.0, 2.0])),
    ("2.36", "Breakout + time-delay confirm", breakout_confirm, _g(N=[12, 20, 40], conf=["delay"], days=[3, 6])),
    ("2.37", "Breakout + 3-thrust-day confirm", breakout_confirm, _g(N=[12, 20, 40], conf=["thrust"])),
    ("2.38", "Price/MA + MA-direction filter", price_ma, _g(N=[10, 20, 35, 50, 100], ma=["sma"], filter=[True])),
    ("2.39", "Volatility-adjusted breakout", breakout, _g(volmap=["direct", "inverse"])),
    ("2.40", "Asymmetric buy/sell breakout", breakout, [{"N": a, "Ns": b} for a, b in ((40, 20), (60, 30), (100, 50), (20, 40))]),
    ("2.41", "Breakout + pyramiding", pyramid, _g(N=[20, 40, 60])),
    ("2.42", "Breakout + trade-exit rule", breakout_exit, [{"N": N, "M": M, "as_reversal": r} for N in (40, 60, 100) for M in (N // 4, N // 2) for r in (False, True)]),
    ("2.43", "Fading minimum move", fade_move, _g(k=[4.0, 6.0, 8.0, 10.0], stop=[False, True])),
    ("2.44", "Fading minimum move + thrust", fade_move, _g(k=[4.0, 6.0, 8.0, 10.0], thrust=[True])),
    ("2.46", "Wide-ranging-day system", wrd_system, WRD_SETS),
    ("2.47", "Run-day breakout", run_breakout, RUN_BO_SETS),
    ("2.48", "Run-day consecutive count", run_count, RUN_CT_SETS),
    ("2.50", "Seasonal index", seasonal, _g(mode=["sign", "extremes"])),
    ("2.51", "Jobs-report response", jobs_report, _g(mode=["follow", "fade"], hold=[2, 5, 10])),
    ("2.55", "Narrow range near top of broad range", narrow_top, _g(L=[5, 10, 15], w=[2.0, 3.0])),
    ("2.56", "Near-vertical move continuation", vertical, _g(k=[3.0, 4.0, 5.0], hold=[10, 20])),
    ("2.58", "Breakout + exit rules (Fri/day-1/vol)", exit_pack, _g(N=[20, 40], rule=["friday", "day1", "vol"])),
]
NAMES = {b[0]: b[1] for b in BOOK}
