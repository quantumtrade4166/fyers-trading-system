import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import itertools
import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR         = Path("G:/fyers_data_pipeline/data")
OUT_DIR          = Path(__file__).parent / "results"
OUT_DIR.mkdir(exist_ok=True)

STARTING_CAPITAL = 1_000_000
DAILY_RISK_PCT   = 0.01
TRADES_PER_DAY   = 5
OH_TOLERANCE     = 0.001
SL_BUFFER        = 0.001
LOAD_FROM        = "2025-12-01"     # extra history so the first day has a previous close
START_DATE       = pd.Timestamp("2026-01-01")
END_DATE         = "2026-06-30"
LAST_ENTRY_HHMM  = 14 * 60 + 30
EOD_HHMM         = 15 * 60 + 15

RANKINGS      = ["file", "body", "gap"]
TARGETS       = [2.0, 3.0, None]          # None = no target, hold to SL or 15:15
NIFTY_FILTERS = ["none", "nifty_red_open", "nifty_gap_down"]


def load_5min(folder: Path) -> pd.DataFrame | None:
    frames = [pd.read_parquet(f) for f in sorted(folder.glob("*/ohlcv_5min.parquet"))]
    if not frames:
        return None
    df = pd.concat(frames)
    if "datetime" in df.columns:
        df = df.set_index("datetime")
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[LOAD_FROM:END_DATE]
    return df if len(df) >= 20 else None


def nifty_day_flags() -> dict:
    df = load_5min(DATA_DIR / "NSE_NIFTY50_INDEX")
    g = df.groupby(df.index.date)
    day = pd.DataFrame({"open": g["open"].first(), "first_close": g["close"].first(),
                        "close": g["close"].last()})
    day["prev_close"] = day["close"].shift(1)
    return {
        d: {"nifty_red_open": r.first_close < r.open,
            "nifty_gap_down": pd.notna(r.prev_close) and r.open < r.prev_close}
        for d, r in day.iterrows()
    }


def trade_cost(pos_val: np.ndarray) -> np.ndarray:
    exch = 0.0000297 * 2 * pos_val
    brokerage = 40.0
    return 0.00025 * pos_val + exch + 0.00003 * pos_val + brokerage + 0.18 * (exch + brokerage)


def scan_signals(all_data: dict, target_rr: float | None) -> pd.DataFrame:
    rows = []
    for sym, df in all_data.items():
        o = df["open"].values.astype(float)
        h = df["high"].values.astype(float)
        l = df["low"].values.astype(float)
        c = df["close"].values.astype(float)
        hhmm  = df.index.hour * 60 + df.index.minute
        dates = np.array(df.index.date)
        n = len(df)

        day_close = pd.Series(c, index=dates).groupby(level=0).last()
        prev_close_map = day_close.shift(1).to_dict()

        with np.errstate(invalid="ignore", divide="ignore"):
            gap = np.where(o > 0, (h - o) / o, 1.0)
        mask = (gap <= OH_TOLERANCE) & (c < h) & (hhmm <= LAST_ENTRY_HHMM) & (df.index >= START_DATE)

        seen = set()
        for i in np.where(mask)[0]:
            d = dates[i]
            if d in seen or i + 1 >= n or dates[i + 1] != d:
                continue
            seen.add(d)

            entry = o[i + 1]
            sl = h[i] * (1 + SL_BUFFER)
            sl_dist = sl - entry
            if entry <= 0 or sl_dist <= 0:
                continue
            target = entry - target_rr * sl_dist if target_rr else -np.inf

            exit_p = exit_r = exit_i = None
            j = i + 1
            while j < n and dates[j] == d:
                if h[j] >= sl:
                    exit_p, exit_r, exit_i = sl, "sl", j; break
                if l[j] <= target:
                    exit_p, exit_r, exit_i = target, "target", j; break
                if hhmm[j] >= EOD_HHMM:
                    exit_p, exit_r, exit_i = c[j], "eod", j; break
                j += 1
            if exit_p is None:
                exit_i = j - 1
                exit_p, exit_r = c[exit_i], "eod"

            pc = prev_close_map.get(d)
            rows.append({
                "symbol":      sym.replace("NSE_", "").replace("_EQ", ""),
                "date":        d,
                "entry_dt":    df.index[i + 1],
                "exit_dt":     df.index[exit_i],
                "entry":       entry,
                "sl_dist":     sl_dist,
                "exit":        exit_p,
                "exit_reason": exit_r,
                "body_pct":    (o[i] - c[i]) / o[i] * 100,
                "gap_pct":     (pc - o[i]) / pc * 100 if pc else np.nan,
            })
    return pd.DataFrame(rows)


def run_portfolio(sig: pd.DataFrame, ranking: str, nifty_filter: str, flags: dict) -> dict:
    if nifty_filter != "none":
        sig = sig[sig["date"].map(lambda d: flags.get(d, {}).get(nifty_filter, False))]

    # causal ranking: earliest entry first; among same-time signals, best quality first
    if ranking == "file":
        sig = sig.sort_values(["entry_dt", "symbol"])
    elif ranking == "body":
        sig = sig.sort_values(["entry_dt", "body_pct"], ascending=[True, False])
    else:
        sig = sig.assign(_g=sig["gap_pct"].fillna(-np.inf)) \
                 .sort_values(["entry_dt", "_g"], ascending=[True, False])

    equity = STARTING_CAPITAL
    trades, eq_by_day = [], {}
    for d, grp in sig.groupby("date", sort=True):
        pick = grp.head(TRADES_PER_DAY)
        risk_each = equity * DAILY_RISK_PCT / TRADES_PER_DAY
        qty  = (risk_each / pick["sl_dist"]).astype(int)
        pick = pick.assign(qty=qty)[qty > 0]
        if pick.empty:
            continue
        pos_val = pick["qty"] * pick["entry"]
        gross   = (pick["entry"] - pick["exit"]) * pick["qty"]
        cost    = trade_cost(pos_val.values)
        pick = pick.assign(pos_val=pos_val, gross=gross, cost=cost, net=gross - cost)
        equity += pick["net"].sum()
        eq_by_day[pd.Timestamp(d)] = equity
        trades.append(pick)

    tr = pd.concat(trades) if trades else pd.DataFrame()
    eq = pd.Series(eq_by_day).sort_index()
    return {"trades": tr, "equity": eq}


def summarise(res: dict) -> dict:
    tr, eq = res["trades"], res["equity"]
    if tr.empty:
        return {}
    curve = pd.concat([pd.Series([STARTING_CAPITAL], index=[START_DATE - pd.Timedelta(days=1)]), eq])
    dd = ((curve - curve.cummax()) / curve.cummax() * 100).min()
    wins = tr[tr["net"] > 0]; losses = tr[tr["net"] <= 0]
    pf = wins["net"].sum() / abs(losses["net"].sum()) if len(losses) else np.inf
    monthly = tr.groupby(pd.to_datetime(tr["date"]).dt.to_period("M"))["net"].sum()
    daily_exp = tr.groupby("date")["pos_val"].sum()
    return {
        "days":      tr["date"].nunique(),
        "trades":    len(tr),
        "gross":     tr["gross"].sum(),
        "costs":     tr["cost"].sum(),
        "net":       tr["net"].sum(),
        "max_dd":    dd,
        "win_rate":  len(wins) / len(tr) * 100,
        "pf":        pf,
        "avg_net":   tr["net"].mean(),
        "green_mo":  f"{(monthly > 0).sum()}/{len(monthly)}",
        "max_lev":   daily_exp.max() / STARTING_CAPITAL,
    }


if __name__ == "__main__":
    sym_dirs = sorted(d for d in DATA_DIR.iterdir()
                      if d.is_dir() and d.name.startswith("NSE_") and d.name.endswith("_EQ"))
    print(f"Loading {len(sym_dirs)} symbols...")
    all_data = {d.name: df for d in sym_dirs if (df := load_5min(d)) is not None}
    flags = nifty_day_flags()
    print(f"Loaded {len(all_data)} symbols.  Nifty days: {len(flags)}\n")

    signals = {}
    for rr in TARGETS:
        label = f"1:{rr:g}" if rr else "no target"
        signals[rr] = scan_signals(all_data, rr)
        print(f"  scanned target {label:<10} → {len(signals[rr])} signals")

    rows, results = [], {}
    for ranking, rr, nf in itertools.product(RANKINGS, TARGETS, NIFTY_FILTERS):
        res = run_portfolio(signals[rr], ranking, nf, flags)
        s = summarise(res)
        if not s:
            continue
        key = (ranking, rr, nf)
        results[key] = res
        rows.append({"ranking": ranking, "target": f"1:{rr:g}" if rr else "none",
                     "nifty": nf, **s})

    tbl = pd.DataFrame(rows).sort_values("net", ascending=False)
    tbl.to_csv(OUT_DIR / "oh_short_v3_grid.csv", index=False)

    print(f"\n{'='*132}")
    print("  OPEN=HIGH SHORT v3  |  Jan-Jun 2026  |  5 trades/day, 1% daily risk  |  NET of costs (no slippage)")
    print(f"{'='*132}")
    print(f"  {'Ranking':<6} {'Target':<7} {'Nifty filter':<15} {'Days':>5} {'Trades':>6} "
          f"{'Gross':>10} {'Costs':>9} {'NET':>10} {'MaxDD':>7} {'Win%':>6} {'PF':>5} "
          f"{'Avg/trd':>8} {'Green mo':>8} {'MaxLev':>7}")
    print("  " + "-" * 128)
    for _, r in tbl.iterrows():
        print(f"  {r['ranking']:<6} {r['target']:<7} {r['nifty']:<15} {r['days']:>5} {r['trades']:>6} "
              f"{r['gross']:>+10,.0f} {r['costs']:>9,.0f} {r['net']:>+10,.0f} {r['max_dd']:>6.1f}% "
              f"{r['win_rate']:>5.1f}% {r['pf']:>5.2f} {r['avg_net']:>+8,.0f} {r['green_mo']:>8} "
              f"{r['max_lev']:>6.1f}x")

    best = tbl.iloc[0]
    rr_best = None if best["target"] == "none" else float(best["target"].split(":")[1])
    bres = results[(best["ranking"], rr_best, best["nifty"])]
    bt = bres["trades"]
    print(f"\n  BEST: ranking={best['ranking']}  target={best['target']}  nifty={best['nifty']}")
    print("  Monthly net P&L:")
    for p, v in bt.groupby(pd.to_datetime(bt["date"]).dt.to_period("M"))["net"].sum().items():
        print(f"    {p}   Rs {v:>+10,.0f}")
    print("  Exit breakdown:")
    for reason, g in bt.groupby("exit_reason"):
        print(f"    {reason:<7} {len(g):>4} ({len(g)/len(bt)*100:4.1f}%)   net Rs {g['net'].sum():>+10,.0f}")
    bt.to_csv(OUT_DIR / "oh_short_v3_best_trades.csv", index=False)
    print(f"\n  Grid  → {OUT_DIR / 'oh_short_v3_grid.csv'}")
    print(f"  Best trades → {OUT_DIR / 'oh_short_v3_best_trades.csv'}")
