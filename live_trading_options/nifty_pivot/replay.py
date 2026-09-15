"""
replay.py — prove the live strategy core reproduces the backtest, trade for trade.
=================================================================================

Drives core/strategy.py (the exact classes the live engine uses) over historical
minute data and compares every trade with the backtest's saved trade log:

    --source fyers   data/NSE_NIFTY_OPTIONS         vs results/trades_compounded_best.csv
    --source breeze  data/BREEZE_OPTIONS (Jun-Sep)  vs results/trades_breeze_oos.csv

The replay supplies the backtest's execution model (fill = option open at the bar
label, stop = 1-min highs in the bar window) through the core's price_fn/high_fn
hooks, so any difference left over is a difference in DECISION logic — which is
exactly what this test exists to catch.

    python live_trading_options/nifty_pivot/replay.py --source breeze
    python live_trading_options/nifty_pivot/replay.py --source fyers --from 2025-01-01 --to 2025-12-31
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT))

from core.strategy import SignalBuilder, PivotCore                                   # noqa: E402
from backtesting.options_pivot_intraday.signals import build_daily_pivots             # noqa: E402

RESULTS = REPO / "backtesting" / "options_pivot_intraday" / "results"
PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text())
BACKTEST_LOT = 75


def make_loader(source: str):
    if source == "breeze":
        from backtesting.options_pivot_intraday.breeze_adapter import BreezeOptionsLoader
        return BreezeOptionsLoader("NIFTY"), RESULTS / "trades_breeze_oos.csv"
    from backtesting.options_credit_spread.data_loader import OptionsDataLoader
    return OptionsDataLoader(), RESULTS / "trades_compounded_best.csv"


def replay(source: str, d_from: str, d_to: str):
    loader, ref_path = make_loader(source)
    spot = loader.spot_1min_series()

    sb = SignalBuilder(PARAMS["supertrend_period"], PARAMS["supertrend_multiplier"])
    sb.load_series(spot)
    bars = sb.bars()
    bars = bars[bars["dir"] != 0]
    by_day = {d: g for d, g in bars.groupby(bars.index.normalize().strftime("%Y-%m-%d"), sort=False)}

    # tradeable days exactly as the backtest decides them (drops multi-week data gaps)
    bt_pivots = build_daily_pivots(spot)
    days = [d for d in bt_pivots.index if d_from <= d <= d_to]
    print(f"[{source}] replaying {len(days)} days  {days[0]} -> {days[-1]}")

    core = PivotCore(PARAMS)
    out, lvl_mismatch = [], 0
    for day in days:
        day_bars = by_day.get(day)
        if day_bars is None or day_bars.empty:
            continue
        levels, _ = sb.pivots_for(pd.Timestamp(day).date())
        ref = bt_pivots.loc[day]
        if levels is None or abs(levels["r1"] - ref["r1"]) > 1e-6 or abs(levels["s1"] - ref["s1"]) > 1e-6:
            lvl_mismatch += 1
            levels = {k: float(ref[k]) for k in ("pp", "r1", "s1")}

        op = loader.get_day_pivot(day, field="open")
        hp = loader.get_day_pivot(day, field="high")
        if op.empty:
            continue

        def price_fn(strike, opt, _ts=None):
            return None

        core.start_day(day, levels, BACKTEST_LOT)
        for ts, bar in day_bars.iterrows():
            def price_fn(strike, opt, ts=ts):
                col = (strike, opt)
                if col not in op.columns or ts not in op.index:
                    return None
                v = op.at[ts, col]
                return None if pd.isna(v) else float(v)

            def high_fn(strike, opt, t0, t1):
                col = (strike, opt)
                if col not in hp.columns:
                    return None
                w = hp.loc[(hp.index > t0) & (hp.index <= t1), col].dropna()
                return float(w.max()) if len(w) else None

            core.on_bar(ts, bar["close"], bar["dir"], bar["st"], price_fn, high_fn)

        if core.pos is not None:                     # backtest's end-of-bars safety close
            ts = day_bars.index[-1]
            px = price_fn(core.pos["strike"], core.pos["option_type"], ts)
            core._close(ts, px if px is not None else core._intrinsic(
                core.pos["strike"], float(day_bars.iloc[-1]["close"]), core.pos["option_type"]),
                "eod_last_bar", float(day_bars.iloc[-1]["close"]), approx=px is None)
        out.extend(core.trades)

    live = pd.DataFrame(out)
    ref = pd.read_csv(ref_path)
    ref["date"] = ref["date"].astype(str)
    ref = ref[(ref["date"] >= d_from) & (ref["date"] <= d_to)].copy()
    return live, ref, lvl_mismatch


def compare(live: pd.DataFrame, ref: pd.DataFrame, lvl_mismatch: int) -> bool:
    key = ["entry_time", "side", "strike"]
    live = live.copy()
    ref = ref.copy()
    for df in (live, ref):
        df["entry_time"] = pd.to_datetime(df["entry_time"]).dt.strftime("%Y-%m-%d %H:%M")
        df["exit_time"] = pd.to_datetime(df["exit_time"]).dt.strftime("%Y-%m-%d %H:%M")
        df["strike"] = df["strike"].astype(int)
    live["gross75"] = (live["entry_price"] - live["exit_price"]) * BACKTEST_LOT

    m = live.merge(ref, on=key, how="outer", suffixes=("_live", "_bt"), indicator=True)
    only_live = m[m["_merge"] == "left_only"]
    only_bt = m[m["_merge"] == "right_only"]
    both = m[m["_merge"] == "both"].copy()

    both["d_entry"] = (both["entry_price_live"] - both["entry_price_bt"]).abs()
    both["d_exit"] = (both["exit_price_live"] - both["exit_price_bt"]).abs()
    both["same_exit_time"] = both["exit_time_live"] == both["exit_time_bt"]
    both["same_reason"] = both["exit_reason_live"] == both["exit_reason_bt"]
    both["d_pnl"] = (both["gross75"] - both["pnl_rupees"]).abs()

    print("\n" + "=" * 78)
    print("PARITY: live strategy core vs backtest")
    print("=" * 78)
    print(f"  trades  live {len(live):5d} | backtest {len(ref):5d}")
    print(f"  matched on (entry_time, side, strike): {len(both)}")
    print(f"  only in live: {len(only_live)} | only in backtest: {len(only_bt)}")
    print(f"  pivot level mismatches: {lvl_mismatch}")
    if len(both):
        print(f"  entry price  max |diff| {both['d_entry'].max():.6f}")
        print(f"  exit price   max |diff| {both['d_exit'].max():.6f}")
        print(f"  exit time    identical {both['same_exit_time'].mean() * 100:.2f}%")
        print(f"  exit reason  identical {both['same_reason'].mean() * 100:.2f}%")
        print(f"  gross P&L    max |diff| Rs {both['d_pnl'].max():.4f}   "
              f"total live Rs {both['gross75'].sum():,.0f} vs backtest Rs {both['pnl_rupees'].sum():,.0f}")
    bad = both[(~both["same_exit_time"]) | (~both["same_reason"]) | (both["d_exit"] > 1e-4)]
    if len(only_live) or len(only_bt) or len(bad):
        cols = ["entry_time", "side", "strike", "exit_time_live", "exit_time_bt",
                "exit_reason_live", "exit_reason_bt"]
        print("\n  first differences:")
        if len(bad):
            print(bad[cols].head(10).to_string(index=False))
        if len(only_live):
            print("  only live:\n", only_live[key].head(5).to_string(index=False))
        if len(only_bt):
            print("  only backtest:\n", only_bt[key].head(5).to_string(index=False))
    ok = (len(only_live) == 0 and len(only_bt) == 0 and len(bad) == 0 and lvl_mismatch == 0)
    print(f"\n  RESULT: {'PASS — identical trades' if ok else 'DIFFERENCES FOUND'}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["breeze", "fyers"], default="breeze")
    ap.add_argument("--from", dest="d_from", default="2026-06-01")
    ap.add_argument("--to", dest="d_to", default="2026-09-11")
    a = ap.parse_args()
    live, ref, lm = replay(a.source, a.d_from, a.d_to)
    raise SystemExit(0 if compare(live, ref, lm) else 1)
