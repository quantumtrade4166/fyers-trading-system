"""
backtest/vwap_split_2026.py — 'split-leg' exit test for the BTC VWAP strangle (ist_day).
=======================================================================================

Step 1: run the normal ist_day backtest for each day (vwap_history.run_cycle, the
LIVE rule code, measured median spreads, bad-fill guard off — the setting that
tracked live best). That gives the day's entries and exits.

Step 2: replay those exact trades with a different exit (same method as the
2026-10-03 replay of the 16 live paper days):
    both  current rules — a close above VWAP buys back BOTH legs
    C     buy back only the BUSTED leg (rose most since sold); the kept leg exits
          when its mark rises 60% above its lowest mark since the split
    D     same split; the kept leg exits when its own 5-min candle closes above
          its own VWAP (typical price, volume-weighted, from 09:30)
    The next entry re-sells only the closed leg; the pair is then a normal strangle.
Each with max 3 / max 4 entries, with and without a -$70 day MTM stop.

Entry timing does not depend on the exit style: the rule code only re-arms after
an exit, and the busted leg is bought back at exactly the moment the normal exit
happens. So the entries are the baseline's entries in every variant.

Prices nobody recorded (kept-leg exit, stop) = mark + half the measured spread
(buying back at the ask). Fees as vwap_history.fee_for.

    .venv\\Scripts\\python.exe live_trading_options\\delta_btc\\backtest\\vwap_split_2026.py --workers 6
"""

import os
os.environ.setdefault("BTC_GUARD", "off")
os.environ.setdefault("BTC_SPREAD", "median")

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import argparse
from multiprocessing import Pool

import pandas as pd

import vwap_history as H

TRAIL = 0.60
STOP = 70.0
MODES = ("both", "C", "D")
OUT = H.OUT


def leg_frames(df, pick, day):
    out = {}
    for leg, ot, k in (("ce", "CE", pick["ce"]), ("pe", "PE", pick["pe"])):
        m = df[(df.strike == k) & (df.opt_type == ot)].set_index("time_ist").sort_index()
        m = pd.DataFrame({"mo": m.mark_open, "mh": m.mark_high.fillna(m.mark),
                          "ml": m.mark_low.fillna(m.mark), "mc": m.mark,
                          "vol": m.volume.fillna(0.0)})
        m5 = m[m.index >= pd.Timestamp(day + " 09:30")].resample(
            "5min", label="left", closed="left").agg(
            {"mo": "first", "mh": "max", "ml": "min", "mc": "last", "vol": "sum"}).dropna()
        tp = (m5.mh + m5.ml + m5.mc) / 3
        cv = m5.vol.cumsum()
        m5["vwap"] = ((tp * m5.vol).cumsum() / cv).where(cv > 0, tp.expanding().mean())
        out[leg] = (m, m5)
    return out


def replay(day, trades, fr, spot, mode, max_entries, use_stop):
    half = lambda p: H.spread_for(p) / 2
    fee = lambda p: H.fee_for(max(p, 0), 1000, spot)
    trades = sorted(trades, key=lambda t: t["entry_time"])[:max_entries]
    ev = []
    for t in trades:
        ev.append((pd.Timestamp(t["entry_time"]), 0, "entry", t))
        ev.append((pd.Timestamp(t["exit_time"]), 1, "exit", t))
    ev.sort(key=lambda e: (e[0], e[1]))

    pos = {}            # leg -> {"sell", "low", "split"}
    st = {"realized": 0.0, "kept": None, "stopped": False}

    def mark(leg, ts):
        m = fr[leg][0]
        sub = m.mc[m.index <= ts]
        return float(sub.iloc[-1]) if len(sub) else None

    def close_leg(leg, price):
        p = pos.pop(leg)
        st["realized"] += p["sell"] - price - fee(price)
        if st["kept"] == leg:
            st["kept"] = None

    def mtm():
        return st["realized"] + sum(p["sell"] - (mk + half(mk)) - fee(mk + half(mk))
                                    for leg, p in pos.items()
                                    if (mk := mark(leg, ts)) is not None)

    ei = 0
    for ts in pd.date_range(day + " 09:30", day + " 17:10", freq="1min"):
        kept = st["kept"]
        if kept and not st["stopped"] and mode != "both":
            m, m5 = fr[kept]
            p = pos[kept]
            prev = ts - pd.Timedelta(minutes=1)
            if mode == "C" and prev in m.index and prev > p["split"]:
                bar = m.loc[prev]
                level = p["low"] * (1 + TRAIL)
                if bar.mh >= level:
                    px = max(level, bar.mo)
                    close_leg(kept, px + half(px))
                else:
                    p["low"] = min(p["low"], bar.mc)
            if mode == "D" and ts.minute % 5 == 0:
                cs = ts - pd.Timedelta(minutes=5)
                if cs in m5.index and ts > p["split"]:
                    c = m5.loc[cs]
                    if c.mc > c.vwap:
                        close_leg(kept, float(c.mc) + half(float(c.mc)))
        while ei < len(ev) and ev[ei][0] < ts + pd.Timedelta(minutes=1):
            t_ev, _, kind, t = ev[ei]
            ei += 1
            if st["stopped"]:
                continue
            if kind == "entry":
                for leg in ("ce", "pe"):
                    if leg not in pos:
                        px = t[leg + "_sell"]
                        st["realized"] -= fee(px)
                        pos[leg] = {"sell": px, "low": px, "split": None}
                st["kept"] = None
            else:
                if not pos:
                    continue
                if mode == "both" or t["exit_reason"] != "close above VWAP" or len(pos) == 1:
                    for leg in list(pos):
                        close_leg(leg, t[leg + "_exit"])
                else:
                    rise = {leg: t[leg + "_exit"] - pos[leg]["sell"] for leg in pos}
                    bust = max(rise, key=rise.get)
                    keep = "pe" if bust == "ce" else "ce"
                    close_leg(bust, t[bust + "_exit"])
                    st["kept"] = keep
                    pos[keep]["split"] = t_ev
                    pos[keep]["low"] = mark(keep, t_ev) or t[keep + "_exit"]
        if use_stop and pos and not st["stopped"] and mtm() <= -STOP:
            for leg in list(pos):
                mk = mark(leg, ts)
                close_leg(leg, mk + half(mk))
            st["stopped"] = True
    sq = pd.Timestamp(day + " 17:10")
    for leg in list(pos):
        mk = mark(leg, sq)
        close_leg(leg, mk + half(mk))
    return round(st["realized"], 4)


def run_day(day):
    try:
        df = H.load_day(day)
        if df is None or df["time_ist"].nunique() < 600:
            return day, {"skip": "thin day"}
        if H.bad_marks(df) > 0.002:
            return day, {"skip": "broken marks"}
        r = H.run_cycle("ist_day", day, df)
        if r.get("skip"):
            return day, {"skip": r["skip"]}
        spot = float(df["spot"].median())
        fr = leg_frames(df, r, day)
        out = {"trades": len(r["trades"]), "baseline_net": r["net"]}
        for me in (3, 4):
            for s in (False, True):
                for m in MODES:
                    out[f"{m}|{me}|{'stop' if s else 'nostop'}"] = replay(
                        day, r["trades"], fr, spot, m, me, s)
        return day, out
    except Exception as e:
        return day, {"skip": f"{type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", default="2026")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    days = sorted(p.stem for p in (H.DATA / "raw").glob(f"{a.year}-*.parquet"))
    if a.only:
        days = [d for d in days if d in set(a.only)]
    OUT.mkdir(parents=True, exist_ok=True)
    res_f = OUT / f"vwap_split_{a.year}.json"
    done = json.loads(res_f.read_text(encoding="utf-8")) if res_f.exists() else {}
    todo = [d for d in days if d not in done]
    print(f"  {len(days)} days, {len(todo)} to run", flush=True)
    with Pool(a.workers) as pool:
        for i, (day, out) in enumerate(pool.imap_unordered(run_day, todo), 1):
            done[day] = out
            if i % 10 == 0 or i == len(todo):
                res_f.write_text(json.dumps(done), encoding="utf-8")
            s = out.get("skip") or (f"{out['trades']}t base {out['baseline_net']:+.2f} "
                                    f"D|4|stop {out['D|4|stop']:+.2f}")
            print(f"  [{i}/{len(todo)}] {day}  {s}", flush=True)
    res_f.write_text(json.dumps(done), encoding="utf-8")
    print(f"  written {res_f}")


if __name__ == "__main__":
    main()
