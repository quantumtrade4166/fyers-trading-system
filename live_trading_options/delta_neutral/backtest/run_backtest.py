"""
backtest/run_backtest.py — historical paper backtest of the delta-neutral strangle.
===================================================================================

Drives the REAL live engine (`live.controller.DNController`) in shadow/paper mode
over historical 1-minute option data, so the backtest is the exact same strategy
code that trades live — no re-implementation to drift out of sync. The paper
executor fills at the touch and simulates its own stops, with `kite=None` it never
touches the network, and `shadow=True` is the untouched benchmark book (ignores
arm/kill, runs the whole day).

Data: ICICI Breeze NIFTY 1-min options (`data/BREEZE_OPTIONS/NIFTY/2026/
ohlcv_1minute.parquet`, Jan–Nov 2026) + the 1-min NIFTY index for spot. Each
trade day (0/1-DTE only, NIFTY expiry Tuesday) is replayed minute by minute at the
bar CLOSE; stops are therefore evaluated on 1-minute marks (a spike that reverts
within the minute is not seen — this is a 1-min-granularity approximation, same as
the strangle V2 rebuild).

SENSEX is not backtested here: only one day of BSESEN option data exists locally.

Run:
    .venv\\Scripts\\python.exe live_trading_options/delta_neutral/backtest/run_backtest.py
    ... --start 2026-06-01 --end 2026-10-31
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import io
import json
import argparse
import datetime as dt
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]          # .../delta_neutral
sys.path.insert(0, str(ROOT))

import live.controller as C                          # the real engine

# ── isolation: a backtest must not read the live control file, write the live
#    audit log, or persist over the live state. shadow=True already skips audit,
#    but patch defensively so a stray path can never leak into real trading data.
C.read_control = lambda *a, **k: {}
C.audit_log = lambda *a, **k: None

PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text())
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

REPO = ROOT.parents[1]                               # fyers_data_pipeline
OPT_PATH = REPO / "data" / "BREEZE_OPTIONS" / "NIFTY" / "2026" / "ohlcv_1minute.parquet"
IDX_PATH = REPO / "data" / "BREEZE_OPTIONS" / "NIFTY" / "index_1min.parquet"
OUT_DIR = Path(__file__).resolve().parent / "results"
OUT_DIR.mkdir(parents=True, exist_ok=True)

OTYPE = {"CALL": "CE", "PUT": "PE"}


# ── data loading ──────────────────────────────────────────────────────────
def load_data(start: dt.date, end: dt.date):
    """Return (opt, spot) for the window. `opt` indexed for fast per-day slicing;
    `spot` is a minute→price Series from the 1-min index."""
    opt = pd.read_parquet(OPT_PATH, columns=["datetime", "expiry", "strike_price",
                                             "option_type", "close", "date"])
    opt["d"] = pd.to_datetime(opt["date"]).dt.date
    opt["exp"] = pd.to_datetime(opt["expiry"]).dt.date
    opt = opt[(opt["d"] >= start) & (opt["d"] <= end)].copy()
    opt["side"] = opt["option_type"].astype(str).map(OTYPE)
    opt["strike_price"] = opt["strike_price"].astype(int)
    opt["dtm"] = pd.to_datetime(opt["datetime"])

    idx = pd.read_parquet(IDX_PATH, columns=["datetime", "close"])
    idx["dtm"] = pd.to_datetime(idx["datetime"])
    idx = idx[(idx["dtm"].dt.date >= start) & (idx["dtm"].dt.date <= end)]
    spot = idx.set_index("dtm")["close"].sort_index()
    return opt, spot


def trade_days(opt: pd.DataFrame, spot: pd.Series, start: dt.date, end: dt.date):
    """Every (day, dte, expiry) NIFTY trades in the window. DTE is measured in
    TRADING days off the real calendar (so an NSE holiday the week of expiry is
    handled), and each day is paired with its FRONT weekly — the nearest expiry on
    or after it that actually has option rows that day. One row per day."""
    trading = sorted({d.date() for d in spot.index})
    tidx = {d: i for i, d in enumerate(trading)}
    have = opt.groupby("exp")["d"].agg(lambda s: set(s))     # expiry -> dates with rows
    expiries = sorted(have.index)
    out = []
    for d in trading:
        if not (start <= d <= end):
            continue
        front = next((E for E in expiries if E >= d and d in have[E]), None)
        if front is None or front not in tidx:
            continue
        dte = tidx[front] - tidx[d]                           # 0 = expiry day, 1 = day before
        if dte in (0, 1):
            out.append({"day": d, "dte": dte, "expiry": front})
    out.sort(key=lambda r: (r["day"], r["dte"]))
    return out


# ── one day ─────────────────────────────────────────────────────────────────
def run_day(day: dt.date, dte: int, expiry: dt.date, opt: pd.DataFrame,
            spot: pd.Series) -> dict:
    rows = opt[(opt["d"] == day) & (opt["exp"] == expiry)]
    if rows.empty:
        return {"date": str(day), "dte": dte, "expiry": str(expiry),
                "traded": False, "realized": 0.0, "reason": "no option rows"}

    # per-minute chain: {timestamp: {(strike,'CE'|'PE'): close}}
    chains: dict = {}
    for ts, strike, side, px in zip(rows["dtm"], rows["strike_price"],
                                    rows["side"], rows["close"]):
        if side is None or pd.isna(px):
            continue
        chains.setdefault(ts, {})[(int(strike), side)] = float(px)

    # spot for this day, forward-filled onto each option timestamp
    sday = spot[(spot.index.date == day)]
    if sday.empty:
        return {"date": str(day), "dte": dte, "expiry": str(expiry),
                "traded": False, "realized": 0.0, "reason": "no spot"}
    smark = sday.reindex(sorted(chains)).ffill().bfill()

    ctrl = C.DNController("NIFTY", str(day), expiry, dte, params=PARAMS,
                          lot_size=PARAMS["lot_sizes"]["NIFTY"], kite=None,
                          symbol_lookup=lambda s, o, E=expiry: f"NIFTY{E:%y%m%d}{s}{o}",
                          shadow=True)
    ctrl.persist = lambda: None            # no disk churn, no live-state writes
    ctrl._write_tick = lambda: None

    for ts in sorted(chains):
        sp = smark.get(ts)
        if sp is None or pd.isna(sp):
            continue
        ctrl.on_tick(chains[ts], float(sp), ts.to_pydatetime())

    pos = ctrl.position
    stops = sum(1 for e in ctrl.events if e["type"] == "stop_hit")
    entries = sum(1 for e in ctrl.events if e["type"] == "entry_complete")
    return {"date": str(day), "dte": dte, "expiry": str(expiry), "traded": entries > 0,
            "realized": round(pos.realized(), 2), "fresh_entries": ctrl.fresh_entries,
            "stop_hits": stops, "closed_legs": len(pos.history),
            "killed": ctrl.killed, "kill_reason": ctrl.kill_reason,
            "mtm_series": [{"t": p["t"], "rupees": p["rupees"]} for p in ctrl.mtm_series],
            "events": [{"t": e["t"], "type": e["type"], **{k: v for k, v in e.items()
                        if k in ("strike", "side", "entry", "exit", "reason", "pnl")}}
                       for e in ctrl.events]}


# ── driver ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-10-31")
    a = ap.parse_args()
    start = dt.date.fromisoformat(a.start)
    end = dt.date.fromisoformat(a.end)

    print(f"Loading Breeze NIFTY options {start} → {end} …")
    opt, spot = load_data(start, end)
    days = trade_days(opt, spot, start, end)
    print(f"{len(days)} trade days (0/1-DTE).  Replaying the live engine in paper mode…\n")

    results, details = [], {}
    cum = 0.0
    print(f"{'date':12}{'dte':>4}{'realized':>11}{'cum':>11}  {'ent':>3}{'stp':>4}  note")
    for d in days:
        r = run_day(d["day"], d["dte"], d["expiry"], opt, spot)
        details[f"{r['date']}_{r['dte']}"] = r
        cum += r["realized"]
        results.append({"date": r["date"], "dte": r["dte"], "expiry": r["expiry"],
                        "realized": r["realized"], "traded": r.get("traded", False),
                        "fresh_entries": r.get("fresh_entries"), "stop_hits": r.get("stop_hits"),
                        "closed_legs": r.get("closed_legs"), "kill_reason": r.get("kill_reason")})
        note = r.get("reason") or (r.get("kill_reason") or "")
        print(f"{r['date']:12}{r['dte']:>4}{r['realized']:>11.2f}{cum:>11.2f}"
              f"  {r.get('fresh_entries',0) or 0:>3}{r.get('stop_hits',0) or 0:>4}  {note}")

    df = pd.DataFrame(results)
    traded = df[df["traded"]]
    wins = (traded["realized"] > 0).sum()
    peak = cummax = 0.0
    curve = []
    for _, row in df.iterrows():
        cummax += row["realized"]
        peak = max(peak, cummax)
        curve.append({"date": row["date"], "dte": int(row["dte"]),
                      "realized": row["realized"], "cumulative": round(cummax, 2),
                      "drawdown": round(cummax - peak, 2)})
    maxdd = min((c["drawdown"] for c in curve), default=0.0)

    daily_csv = OUT_DIR / "dn_nifty_backtest_daily.csv"
    df.to_csv(daily_csv, index=False)
    (OUT_DIR / "dn_nifty_backtest_curve.json").write_text(json.dumps(curve, indent=2))
    (OUT_DIR / "dn_nifty_backtest_details.json").write_text(json.dumps(details, indent=2))

    print("\n" + "=" * 60)
    print(f"NIFTY DN paper backtest (1 lot)  {start} → {end}")
    print(f"  trade days          : {len(df)}  (traded {len(traded)}, flat/blocked {len(df)-len(traded)})")
    print(f"  cumulative P&L      : ₹{cum:,.2f}")
    print(f"  peak / max drawdown : ₹{peak:,.2f} / ₹{maxdd:,.2f}")
    print(f"  win rate (traded)   : {round(100*wins/len(traded)) if len(traded) else 0}%  ({wins}/{len(traded)})")
    if len(traded):
        print(f"  best / worst day    : ₹{traded['realized'].max():,.2f} / ₹{traded['realized'].min():,.2f}")
        print(f"  by DTE              : "
              + "  ".join(f"{d}-DTE ₹{traded[traded['dte']==d]['realized'].sum():,.0f} "
                          f"({(traded['dte']==d).sum()}d)" for d in (0, 1)))
    print(f"\n  saved: {daily_csv}")
    print(f"         {OUT_DIR / 'dn_nifty_backtest_curve.json'}")
    print(f"         {OUT_DIR / 'dn_nifty_backtest_details.json'}")


if __name__ == "__main__":
    main()
