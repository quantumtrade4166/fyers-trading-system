"""Replay the ist_day PAPER trades with a 'split-leg' exit.

Same entries and the same fill prices the paper engine got. Only change: on a
'close above VWAP' exit, buy back only the BUSTED leg (the one that rose most
since it was sold) and keep the other. The kept leg then exits by:
  C  = trailing stop: mark rises 60% above its lowest mark since the split
  D  = its own 5-min candle closes above its own VWAP
  or at square-off. The next paper entry re-sells only the closed leg (at the
paper fill), after which the pair is a normal strangle again.

Prices the paper never recorded (the kept leg's exit) = Delta 1-min MARK +
half the measured spread (buying back at the ask), fee 3.5% of premium.
"""
import sys, json, glob
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")

SPREAD = json.load(open(r"G:\fyers_data_pipeline\live_trading_options\delta_btc\config\spread_model.json"))
L = pd.read_parquet("legs.parquet")
TRAIL = 0.60
STOP = 70.0


def half_spread(p):
    e, s = SPREAD["edges"], SPREAD["spread"]
    for i in range(len(e) - 1, -1, -1):
        if p >= e[i]:
            return s[i] / 2
    return s[0] / 2


def fee(p):
    return 0.035 * max(p, 0)


def leg_frames(day):
    out = {}
    x = L[L.date == day]
    for leg in ("ce", "pe"):
        m = x[x.leg == leg].set_index("t").sort_index()
        # own 5-min candles + VWAP for rule D (VWAP from 09:30, typical price)
        m5 = m[m.index >= pd.Timestamp(day + " 09:30")].resample("5min", label="left", closed="left").agg(
            {"mo": "first", "mh": "max", "ml": "min", "mc": "last", "vol": "sum"}).dropna()
        tp = (m5.mh + m5.ml + m5.mc) / 3
        cv = m5.vol.cumsum()
        m5["vwap"] = ((tp * m5.vol).cumsum() / cv).where(cv > 0, tp.expanding().mean())
        out[leg] = (m, m5)
    return out


def run(d, mode, max_entries, use_stop, cap=None):
    """mode: 'both' (paper rules) | 'C' | 'D'. Returns day P&L and event notes."""
    day = d["date"]
    fr = leg_frames(day)
    trades = sorted(d["trades"], key=lambda t: t["entry_time"])[:max_entries]
    # event list
    ev = []
    for t in trades:
        ev.append((pd.Timestamp(t["entry_time"]), 0, "entry", t))
        ev.append((pd.Timestamp(t["exit_time"]), 1, "exit", t))
    sq = pd.Timestamp(day + " 17:10")
    ev.sort(key=lambda e: (e[0], e[1]))

    pos = {}          # leg -> dict(sell, low, split_time)
    realized = 0.0
    notes = []
    kept = None       # leg kept alone after a split
    done_trades = set()   # paper trades already exited early by the VWAP cap
    cur = None            # paper trade currently open
    ce1, pe1 = fr["ce"][0].mc, fr["pe"][0].mc
    comb = (ce1 + pe1).dropna()
    cand = [(pd.Timestamp(c["t"]) + pd.Timedelta(minutes=5), c["vwap"]) for c in d["candles"] if c.get("vwap")]
    caps_fired = []

    def mark(leg, ts):
        m = fr[leg][0]
        sub = m[m.index <= ts.floor("min")]
        return float(sub.mc.iloc[-1]) if len(sub) else None

    def close_leg(leg, price, why, ts):
        nonlocal realized, kept
        p = pos.pop(leg)
        pnl = p["sell"] - price - fee(price)
        realized += pnl
        notes.append(f"{ts:%H:%M} buy {leg.upper()} {price:.1f} ({why}) {pnl:+.1f}")
        if kept == leg:
            kept = None

    def mtm(ts):
        u = 0.0
        for leg, p in pos.items():
            mk = mark(leg, ts)
            if mk is None:
                continue
            bb = mk + half_spread(mk)
            u += p["sell"] - bb - fee(bb)
        return realized + u

    # walk minute by minute between events so kept-leg rules and the stop can fire
    minutes = pd.date_range(day + " 09:30", day + " 17:10", freq="1min")
    ei = 0
    stopped = False
    for ts in minutes:
        # 1) kept-leg rules on the bar that just finished (ts-1min .. ts)
        if kept and not stopped and mode in ("C", "D"):
            leg = kept
            m, m5 = fr[leg]
            p = pos[leg]
            prev = ts - pd.Timedelta(minutes=1)
            if mode == "C" and prev in m.index and prev > p["split"]:
                bar = m.loc[prev]
                level = p["low"] * (1 + TRAIL)
                if bar.mh >= level:
                    px = max(level, bar.mo)
                    close_leg(leg, px + half_spread(px), "C trail 60%", prev)
                else:
                    p["low"] = min(p["low"], bar.mc)
            if mode == "D" and ts.minute % 5 == 0:
                cs = ts - pd.Timedelta(minutes=5)
                if cs in m5.index and ts > p["split"]:
                    c = m5.loc[cs]
                    if c.mc > c.vwap:
                        px = float(c.mc)
                        close_leg(leg, px + half_spread(px), "D own VWAP", ts)
        # 2) paper events at or before this minute
        while ei < len(ev) and ev[ei][0] < ts + pd.Timedelta(minutes=1):
            t_ev, _, kind, t = ev[ei]
            ei += 1
            if stopped:
                continue
            if kind == "entry":
                cur = t
                sold = []
                for leg in ("ce", "pe"):
                    if leg not in pos:
                        px = t[leg + "_sell"]
                        pos[leg] = {"sell": px - fee(px), "low": px, "split": None}
                        realized += 0  # fee already in sell
                        sold.append(f"{leg.upper()} {px:.1f}")
                kept = None   # pair restored -> normal strangle again
                notes.append(f"{t_ev:%H:%M} sell {' + '.join(sold)} (paper #{t['n']})")
            else:
                if t["n"] in done_trades or not pos:
                    continue
                vw = t["exit_reason"] == "close above VWAP"
                if mode == "both" or not vw or len(pos) == 1:
                    for leg in list(pos):
                        close_leg(leg, t[leg + "_exit"], t["exit_reason"], t_ev)
                else:
                    rise = {leg: t[leg + "_exit"] - (pos[leg]["sell"] + fee(0)) for leg in pos}
                    # rise vs original sell price (sell stored net of fee; add it back)
                    rise = {leg: t[leg + "_exit"] - t_sell for leg, t_sell in
                            ((lg, pos[lg]["sell"] / 0.965) for lg in pos)}
                    bust = max(rise, key=rise.get)
                    keep = "pe" if bust == "ce" else "ce"
                    close_leg(bust, t[bust + "_exit"], "busted leg", t_ev)
                    kept = keep
                    pos[keep]["split"] = t_ev
                    pos[keep]["low"] = mark(keep, t_ev) or t[keep + "_exit"]
                    notes.append(f"{t_ev:%H:%M} keep {keep.upper()} (mark {pos[keep]['low']:.1f})")
        # 2b) VWAP cap: combined >= cap x VWAP while the pair is open -> exit now
        if cap and not stopped and len(pos) == 2 and cur is not None and cur["n"] not in done_trades                 and ts in comb.index:
            now = ts + pd.Timedelta(minutes=1)
            vw = [v for (tc, v) in cand if tc <= now]
            if vw and comb[ts] >= cap * vw[-1]:
                caps_fired.append(ts)
                done_trades.add(cur["n"])
                px = {lg: mark(lg, ts) for lg in ("ce", "pe")}
                if mode == "both":
                    for lg in list(pos):
                        close_leg(lg, px[lg] + half_spread(px[lg]), f"cap {comb[ts]:.0f}>=1.5x{vw[-1]:.0f}", now)
                else:
                    rise = {lg: px[lg] - pos[lg]["sell"] / 0.965 for lg in pos}
                    bust = max(rise, key=rise.get)
                    keep = "pe" if bust == "ce" else "ce"
                    close_leg(bust, px[bust] + half_spread(px[bust]), f"cap {comb[ts]:.0f}>=1.5x{vw[-1]:.0f}", now)
                    kept = keep
                    pos[keep]["split"] = now
                    pos[keep]["low"] = px[keep]
                    notes.append(f"{now:%H:%M} keep {keep.upper()} (mark {px[keep]:.1f})")
        # 3) -70 stop on the whole day
        if use_stop and pos and not stopped and mtm(ts) <= -STOP:
            for leg in list(pos):
                mk = mark(leg, ts)
                close_leg(leg, mk + half_spread(mk), "-70 stop", ts)
            stopped = True
    # square-off anything still open (kept leg after the paper went flat)
    for leg in list(pos):
        mk = mark(leg, sq)
        close_leg(leg, mk + half_spread(mk), "square-off", sq)
    return realized, notes, caps_fired


CAP = None
if __name__ == "__main__":
    CAP = float(sys.argv[1]) if len(sys.argv) > 1 else None
    print("VWAP cap:", CAP)
    days = [json.load(open(f)) for f in sorted(glob.glob("ist/2026-*.json"))]
    for me in (3, 4):
        print(f"\n===== max {me} entries =====")
        hdr = f"{'date':11}" + "".join(f"{m+(' stop' if s else ''):>11}" for s in (False, True) for m in ("both", "C", "D"))
        print(hdr)
        tot = {}
        for d in days:
            row = f"{d['date']:11}"
            for s in (False, True):
                for m in ("both", "C", "D"):
                    p = run(d, m, me, s, CAP)[0]
                    tot[(m, s)] = tot.get((m, s), 0) + p
                    row += f"{p:11.2f}"
            print(row)
        print(f"{'TOTAL':11}" + "".join(f"{tot[(m, s)]:11.2f}" for s in (False, True) for m in ("both", "C", "D")))
