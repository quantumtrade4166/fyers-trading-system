"""Independent audit of one backtest year of Nifty ST Directional.

Re-derives every decision from the raw Breeze 1-minute cache with code written
separately from engine.py / candles.py (own 5-min resampling, own Supertrend),
then compares with the engine's trades.csv / reversals.csv.

    python -m backtesting.options_st_directional.audit_year --year 2023 --tag Y2023_10LOTS_BE25_SLIP05
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RES = Path(__file__).parent / "results"
BREEZE = ROOT / "data" / "BREEZE_OPTIONS" / "NIFTY"


# ---------- own implementations ----------
def five_min(m: pd.DataFrame) -> pd.DataFrame:
    m = m[m["datetime"].dt.strftime("%H:%M") < "15:30"].copy()
    m["k"] = m["datetime"].dt.floor("5min")
    g = m.sort_values("datetime").groupby("k")
    return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                         "low": g["low"].min(), "close": g["close"].last()})


def st_tv(c: pd.DataFrame, period=10, mult=3.0) -> pd.DataFrame:
    """TradingView ta.supertrend, written from the Pine reference."""
    h, l, cl = c["high"].values, c["low"].values, c["close"].values
    n = len(c)
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - cl[i - 1]), abs(l[i] - cl[i - 1]))
    atr = np.full(n, np.nan)
    if n >= period:
        atr[period - 1] = tr[:period].mean()
        for i in range(period, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    hl2 = (h + l) / 2
    up = hl2 - mult * atr
    dn = hl2 + mult * atr
    st = np.full(n, np.nan)
    dirn = np.zeros(n)
    fu, fd = up.copy(), dn.copy()
    for i in range(n):
        if np.isnan(atr[i]):
            continue
        if i > 0 and not np.isnan(atr[i - 1]):
            fu[i] = up[i] if (up[i] > fu[i - 1] or cl[i - 1] < fu[i - 1]) else fu[i - 1]
            fd[i] = dn[i] if (dn[i] < fd[i - 1] or cl[i - 1] > fd[i - 1]) else fd[i - 1]
            prev_st = st[i - 1]
            if prev_st == fd[i - 1]:
                dirn[i] = 1 if cl[i] > fd[i] else -1
            else:
                dirn[i] = -1 if cl[i] < fu[i] else 1
        else:
            dirn[i] = -1                       # Pine: na(atr[1]) -> dir = 1 (down)
        st[i] = fu[i] if dirn[i] == 1 else fd[i]
    out = c.copy()
    out["st"], out["dir"] = st, dirn
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--tag", required=True)
    a = ap.parse_args()
    y = a.year
    R = RES / a.tag
    trades = pd.read_csv(R / "trades.csv", parse_dates=["entry_ts", "exit_ts", "signal_ts", "exit_signal_ts", "ref_candle_ts"])
    revs = pd.read_csv(R / "reversals.csv", parse_dates=["ts"])
    be = float(pd.read_json(R / "config.json", typ="series").get("breakeven_after_points") or 0)

    # ---- spot flips ----
    idx = pd.read_parquet(BREEZE / "index_1min.parquet")
    idx["datetime"] = pd.to_datetime(idx["datetime"])
    idx = idx[(idx["datetime"] >= f"{y-1}-11-01") & (idx["datetime"] < f"{y+1}-01-01")]
    sp = st_tv(five_min(idx))
    sp = sp[sp.index >= f"{y}-01-01"]
    flips = []
    for d, g in sp.groupby(sp.index.date):
        g = g[(g.index.strftime("%H:%M") >= "09:15") & (g.index.strftime("%H:%M") <= "15:05")]
        dd = g["dir"].values
        for i in range(1, len(g)):
            if dd[i] != dd[i - 1]:
                flips.append(g.index[i] + pd.Timedelta(minutes=5))
    ours = set(flips)
    eng = set(revs["ts"])
    print(f"SPOT FLIPS  engine {len(eng)}  audit {len(ours)}  identical {len(eng & ours)}")
    for t in sorted(eng ^ ours)[:20]:
        print("   differs:", t, "engine" if t in eng else "audit-only")

    # ---- options ----
    opt = pd.concat([pd.read_parquet(p) for p in sorted((BREEZE).glob("*/ohlcv_1minute.parquet"))
                     if p.parent.name in (str(y - 1), str(y), str(y + 1))])
    opt["datetime"] = pd.to_datetime(opt["datetime"])
    opt["expiry"] = pd.to_datetime(opt["expiry"]).dt.normalize()
    opt["typ"] = opt["option_type"].str[0].map({"C": "CE", "P": "PE"})
    opt = opt.drop_duplicates(["expiry", "strike_price", "typ", "datetime"])
    groups = {k: g.sort_values("datetime") for k, g in opt.groupby(["expiry", "strike_price", "typ"])}
    days = sorted(idx["datetime"].dt.strftime("%Y-%m-%d").unique())

    issues = []
    for tr in trades.itertuples():
        key = (pd.Timestamp(tr.expiry), int(tr.strike), tr.opt_type)
        m = groups.get(key)
        if m is None:
            issues.append((tr.trade_id, "no raw data")); continue
        day = tr.date
        prev = days[days.index(day) - 1]
        m2 = m[(m["datetime"].dt.strftime("%Y-%m-%d") >= prev) & (m["datetime"].dt.strftime("%Y-%m-%d") <= day)]
        c = st_tv(five_min(m2))
        c = c[c.index.strftime("%Y-%m-%d") == day]
        mt = m[m["datetime"].dt.strftime("%Y-%m-%d") == day].set_index("datetime")

        # fills
        def open_at(ts):
            w = mt[(mt.index >= ts) & (mt.index < ts + pd.Timedelta(minutes=5))]
            return (w.index[0], float(w["open"].iloc[0])) if len(w) else (None, None)
        fts, fpx = open_at(pd.Timestamp(tr.earliest_exec_ts))   # signal time, or the exit time of the trade it waited for
        if fpx is None or abs(fpx - tr.entry_price) > 0.01:
            issues.append((tr.trade_id, f"entry fill {tr.entry_price} vs audit {fpx}"))

        # signal: replay reference low from the reference candle
        ref = None
        rc = pd.Timestamp(tr.ref_candle_ts)
        sig_audit = None
        for t, r in c[c.index >= rc].iterrows():
            if t == rc:
                ref = r["low"]; continue
            if r["close"] < ref:
                sig_audit = t + pd.Timedelta(minutes=5); break
            if r["low"] < ref:
                ref = r["low"]
        if sig_audit != tr.signal_ts:
            issues.append((tr.trade_id, f"signal {tr.signal_ts} vs audit {sig_audit}"))

        # exit
        entry_min = pd.Timestamp(tr.entry_ts)
        exit_audit, why = None, None
        armed = None
        stop_hit = None
        for t, r in c[c.index >= entry_min.floor("5min")].iterrows():
            T = t + pd.Timedelta(minutes=5)
            if be:
                for mm, b in mt[(mt.index >= max(t, entry_min.floor("min"))) & (mt.index < T)].iterrows():
                    if armed is not None and b["high"] >= tr.entry_price:
                        stop_hit = mm; break
                    if armed is None and b["low"] <= tr.entry_price - be:
                        armed = mm
                if stop_hit is not None:
                    exit_audit, why = stop_hit, "breakeven_stop"; break
            if T >= pd.Timestamp(f"{day} 15:15"):
                exit_audit, why = pd.Timestamp(f"{day} 15:15"), "square_off_1515"; break
            if t >= entry_min.floor("5min") and T > entry_min and not np.isnan(r["st"]) and r["close"] > r["st"]:
                exit_audit, why = open_at(T)[0], "option_st"; break
        if why != tr.exit_reason or (exit_audit is not None and abs((pd.Timestamp(exit_audit) - tr.exit_ts).total_seconds()) > 60):
            issues.append((tr.trade_id, f"exit {tr.exit_reason}@{tr.exit_ts} vs audit {why}@{exit_audit}"))

    print(f"TRADES checked {len(trades)}  issues {len(issues)}")
    for i in issues[:40]:
        print("  ", i)
    pd.DataFrame(issues, columns=["trade_id", "issue"]).to_csv(R / "audit_issues.csv", index=False)


if __name__ == "__main__":
    main()
