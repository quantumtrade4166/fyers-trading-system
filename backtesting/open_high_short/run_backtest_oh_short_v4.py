import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR       = Path("G:/fyers_data_pipeline/data")
OUT_DIR        = Path(__file__).parent / "results"
OUT_DIR.mkdir(exist_ok=True)

CAPITAL        = 1_000_000
DAILY_RISK     = 0.01
TRADES_PER_DAY = 5
MAX_POS        = 500_000
SLIPPAGE       = 0.0005          # round trip, charged on position value
OH_TOL         = 0.001
SL_BUFFER      = 0.001
EOD_HHMM       = 15 * 60 + 15
LOAD_FROM, LOAD_TO = "2024-05-01", "2026-06-30"
IN_SAMPLE      = ("2024-05-29", "2025-12-31")
OUT_OF_SAMPLE  = ("2026-01-01", "2026-06-30")

VERSIONS = {
    "A": {"min_stop": 0.005, "below_prev_low": False,
          "desc": "15-min O=H + red, gap down, weaker than Nifty, stop >= 0.5%"},
    "B": {"min_stop": 0.005, "below_prev_low": True,
          "desc": "A + 15-min close below previous day low"},
    "C": {"min_stop": 0.010, "below_prev_low": False,
          "desc": "A with stop >= 1.0%"},
}
MIN_STOPS = sorted({v["min_stop"] for v in VERSIONS.values()})


def load_5min(folder: Path) -> pd.DataFrame | None:
    frames = [pd.read_parquet(f) for f in sorted(folder.glob("*/ohlcv_5min.parquet"))]
    if not frames:
        return None
    df = pd.concat(frames)
    if "datetime" in df.columns:
        df = df.set_index("datetime")
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[LOAD_FROM:LOAD_TO]
    return df if len(df) >= 100 else None


def nifty_days() -> dict:
    df = load_5min(DATA_DIR / "NSE_NIFTY50_INDEX")
    hhmm = df.index.hour * 60 + df.index.minute
    d = pd.Series(df.index.date, index=df.index)
    last_close = df["close"].groupby(d.values).last()
    c0925 = df["close"][hhmm == 565].groupby(d[hhmm == 565].values).last()
    prev_close = last_close.shift(1)
    return {day: (prev_close[day], c0925[day]) for day in c0925.index
            if pd.notna(prev_close.get(day))}


def scan_stock(sym: str, df: pd.DataFrame, nifty: dict) -> list[dict]:
    o = df["open"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    hhmm  = np.asarray(df.index.hour * 60 + df.index.minute)
    dates = np.array(df.index.date)
    n = len(df)

    uniq, starts = np.unique(dates, return_index=True)
    ends = np.append(starts[1:], n)

    rows = []
    prev_close = prev_low = None
    for d, s, e in zip(uniq, starts, ends):
        ok_bars = (e - s >= 5 and hhmm[s] == 555 and hhmm[s + 1] == 560
                   and hhmm[s + 2] == 565 and hhmm[s + 3] == 570)
        if prev_close and ok_bars and d in nifty:
            or_open  = o[s]
            or_high  = h[s:s + 3].max()
            or_close = c[s + 2]
            entry    = o[s + 3]
            n_prev, n_0925 = nifty[d]
            rel = (or_close / prev_close - 1) - (n_0925 / n_prev - 1)

            if (or_open > 0 and (or_high - or_open) / or_open <= OH_TOL
                    and or_close < or_open and or_open < prev_close
                    and rel < 0 and 0 < entry < or_high):
                row = {"symbol": sym.replace("NSE_", "").replace("_EQ", ""),
                       "date": d, "entry_dt": df.index[s + 3], "entry": entry,
                       "or_high": or_high, "or_close": or_close,
                       "prev_close": prev_close, "prev_low": prev_low,
                       "gap_pct": (or_open / prev_close - 1) * 100, "rel": rel}
                for ms in MIN_STOPS:
                    stop = max(or_high * (1 + SL_BUFFER), entry * (1 + ms))
                    exit_p, reason, exit_i = c[e - 1], "eod", e - 1
                    for j in range(s + 3, e):
                        if h[j] >= stop:
                            exit_p, reason, exit_i = stop, "sl", j
                            break
                        if hhmm[j] >= EOD_HHMM:
                            exit_p, reason, exit_i = c[j], "eod", j
                            break
                    row[f"stop_{ms}"]   = stop
                    row[f"exit_{ms}"]   = exit_p
                    row[f"reason_{ms}"] = reason
                    row[f"exitdt_{ms}"] = df.index[exit_i]
                rows.append(row)

        prev_close = c[e - 1]
        prev_low   = l[s:e].min()
    return rows


def trade_cost(pos_val):
    exch = 0.0000297 * 2 * pos_val
    return 0.00025 * pos_val + exch + 0.00003 * pos_val + 40 + 0.18 * (exch + 40)


def run(sig: pd.DataFrame, ver: str, period: tuple) -> dict:
    v  = VERSIONS[ver]
    ms = v["min_stop"]
    start, end = (pd.Timestamp(p).date() for p in period)
    s = sig[(sig["date"] >= start) & (sig["date"] <= end)]
    if v["below_prev_low"]:
        s = s[s["or_close"] < s["prev_low"]]
    s = s.sort_values(["date", "rel"])

    equity, trades, eq = CAPITAL, [], {}
    for d, grp in s.groupby("date", sort=True):
        p = grp.head(TRADES_PER_DAY).copy()
        risk_each = equity * DAILY_RISK / TRADES_PER_DAY
        sl_dist = p[f"stop_{ms}"] - p["entry"]
        p["qty"] = np.minimum(risk_each / sl_dist, MAX_POS / p["entry"]).astype(int)
        p = p[p["qty"] > 0]
        if p.empty:
            continue
        p["exit"]        = p[f"exit_{ms}"]
        p["exit_reason"] = p[f"reason_{ms}"]
        p["exit_dt"]     = p[f"exitdt_{ms}"]
        p["pos_val"]     = p["qty"] * p["entry"]
        p["gross"]       = (p["entry"] - p["exit"]) * p["qty"]
        p["cost"]        = trade_cost(p["pos_val"])
        p["slip"]        = p["pos_val"] * SLIPPAGE
        p["net"]         = p["gross"] - p["cost"] - p["slip"]
        equity += p["net"].sum()
        eq[pd.Timestamp(d)] = equity
        trades.append(p)

    cols = ["symbol", "date", "entry_dt", "exit_dt", "entry", "exit", "exit_reason",
            "qty", "pos_val", "gap_pct", "rel", "gross", "cost", "slip", "net"]
    tr = pd.concat(trades)[cols] if trades else pd.DataFrame(columns=cols)
    return {"trades": tr, "equity": pd.Series(eq).sort_index()}


def summarise(res: dict) -> dict:
    tr, eq = res["trades"], res["equity"]
    if tr.empty:
        return {"trades": 0}
    curve = pd.concat([pd.Series([CAPITAL]), eq.reset_index(drop=True)])
    dd = ((curve - curve.cummax()) / curve.cummax() * 100).min()
    w, lo = tr[tr["net"] > 0], tr[tr["net"] <= 0]
    months = tr.groupby(pd.to_datetime(tr["date"]).dt.to_period("M"))["net"].sum()
    return {
        "days": tr["date"].nunique(), "trades": len(tr),
        "gross": tr["gross"].sum(), "costs": tr["cost"].sum(), "slip": tr["slip"].sum(),
        "net": tr["net"].sum(), "max_dd": dd,
        "win": len(w) / len(tr) * 100,
        "pf": w["net"].sum() / abs(lo["net"].sum()) if len(lo) else np.inf,
        "avg": tr["net"].mean(),
        "green": f"{(months > 0).sum()}/{len(months)}",
        "lev": tr.groupby("date")["pos_val"].sum().max() / CAPITAL,
    }


def print_table(title: str, rows: dict):
    print(f"\n{'='*120}\n  {title}\n{'='*120}")
    print(f"  {'Ver':<4} {'Days':>5} {'Trades':>6} {'Gross':>10} {'Costs':>9} {'Slip':>8} {'NET':>10} "
          f"{'NET%':>6} {'MaxDD':>7} {'Win%':>6} {'PF':>5} {'Avg/trd':>8} {'Green mo':>9} {'MaxLev':>7}")
    print("  " + "-" * 116)
    for ver, s in rows.items():
        if s["trades"] == 0:
            print(f"  {ver:<4} no trades")
            continue
        print(f"  {ver:<4} {s['days']:>5} {s['trades']:>6} {s['gross']:>+10,.0f} {s['costs']:>9,.0f} "
              f"{s['slip']:>8,.0f} {s['net']:>+10,.0f} {s['net']/CAPITAL*100:>+5.1f}% {s['max_dd']:>6.1f}% "
              f"{s['win']:>5.1f}% {s['pf']:>5.2f} {s['avg']:>+8,.0f} {s['green']:>9} {s['lev']:>6.1f}x")


def print_detail(res: dict, label: str):
    tr = res["trades"]
    print(f"\n  {label} — monthly net:")
    m = tr.groupby(pd.to_datetime(tr["date"]).dt.to_period("M"))["net"].agg(["sum", "count"])
    for p, r in m.iterrows():
        print(f"    {p}  {int(r['count']):>4} trades   Rs {r['sum']:>+10,.0f}")
    print("  Exits: " + "   ".join(f"{k} {len(g)} (Rs {g['net'].sum():+,.0f})"
                                  for k, g in tr.groupby("exit_reason")))


if __name__ == "__main__":
    sym_dirs = sorted(d for d in DATA_DIR.iterdir()
                      if d.is_dir() and d.name.startswith("NSE_") and d.name.endswith("_EQ"))
    print(f"Loading {len(sym_dirs)} symbols {LOAD_FROM} → {LOAD_TO} ...")
    nifty = nifty_days()
    rows = []
    for d in sym_dirs:
        df = load_5min(d)
        if df is not None:
            rows.extend(scan_stock(d.name, df, nifty))
    sig = pd.DataFrame(rows)
    print(f"Candidate setups (all versions, before 5/day limit): {len(sig)}")
    for name, per in [("in-sample", IN_SAMPLE), ("out-of-sample", OUT_OF_SAMPLE)]:
        a, b = (pd.Timestamp(p).date() for p in per)
        sub = sig[(sig["date"] >= a) & (sig["date"] <= b)]
        print(f"  {name:<14} {len(sub):>6} setups on {sub['date'].nunique()} days")

    print("\nVersions (fixed before running):")
    for k, v in VERSIONS.items():
        print(f"  {k}: {v['desc']}")

    is_res = {k: run(sig, k, IN_SAMPLE) for k in VERSIONS}
    is_sum = {k: summarise(r) for k, r in is_res.items()}
    print_table(f"IN-SAMPLE  {IN_SAMPLE[0]} → {IN_SAMPLE[1]}  (net of costs + 0.05% slippage)", is_sum)

    chosen = max((k for k in is_sum if is_sum[k]["trades"]), key=lambda k: is_sum[k]["net"])
    print(f"\n  Chosen on in-sample: {chosen}  ({VERSIONS[chosen]['desc']})")
    print_detail(is_res[chosen], f"In-sample {chosen}")

    oos = run(sig, chosen, OUT_OF_SAMPLE)
    print_table(f"OUT-OF-SAMPLE  {OUT_OF_SAMPLE[0]} → {OUT_OF_SAMPLE[1]}  — version {chosen} only",
                {chosen: summarise(oos)})
    if not oos["trades"].empty:
        print_detail(oos, f"Out-of-sample {chosen}")

    is_res[chosen]["trades"].to_csv(OUT_DIR / f"oh_short_v4_{chosen}_insample_trades.csv", index=False)
    oos["trades"].to_csv(OUT_DIR / f"oh_short_v4_{chosen}_oos_trades.csv", index=False)
    print(f"\n  Trades saved in {OUT_DIR}")
