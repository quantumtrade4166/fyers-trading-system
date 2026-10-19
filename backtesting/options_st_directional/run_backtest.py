"""Run the NIFTY spot-Supertrend directional option-selling backtest.

    .venv\\Scripts\\python.exe -m backtesting.options_st_directional.run_backtest
    .venv\\Scripts\\python.exe -m backtesting.options_st_directional.run_backtest --start 2026-09-01 --end 2026-09-05 --tag SAMPLE
    .venv\\Scripts\\python.exe -m backtesting.options_st_directional.run_backtest --slippage 0.5 --tag SLIP05

Option bars not cached locally are fetched from Breeze (needs today's session:
python -m options.breeze.session --login).

Outputs -> backtesting/options_st_directional/results/{tag}/
  trades.csv, events.csv, setups.csv, reversals.csv, daily_pnl.csv,
  data_gaps.csv, metrics.json, config.json, summary.png, days/{date}.png
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from backtesting.options_st_directional.config import Config
from backtesting.options_st_directional.data import BreezeProvider
from backtesting.options_st_directional.engine import run_day
from backtesting.options_st_directional import report

RESULTS = Path(__file__).resolve().parent / "results"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=Config.start)
    ap.add_argument("--end", default=Config.end)
    ap.add_argument("--lots", type=int, default=Config.lots)
    ap.add_argument("--capital", type=float, default=Config.capital)
    ap.add_argument("--slippage", type=float, default=Config.slippage_points)
    ap.add_argument("--breakeven", type=float, default=None,
                    help="OPTIONAL: stop to cost once premium is this many points in favour")
    ap.add_argument("--cpr-max", type=float, default=None, help="OPTIONAL: skip days with prev-day CPR width above this")
    ap.add_argument("--rsi", action="store_true", help="OPTIONAL: PE only if 5-min RSI>50, CE only if <50")
    ap.add_argument("--tag", default=Config.tag)
    ap.add_argument("--no-day-charts", action="store_true")
    a = ap.parse_args()
    cfg = Config(start=a.start, end=a.end, lots=a.lots, capital=a.capital,
                 slippage_points=a.slippage, breakeven_after_points=a.breakeven,
                 cpr_max_width=a.cpr_max, rsi_filter=a.rsi, tag=a.tag)

    out = RESULTS / cfg.tag
    (out / "days").mkdir(parents=True, exist_ok=True)

    prov = BreezeProvider(cfg, verbose=True)
    days = prov.days_in_range(cfg.start, cfg.end)
    print(f"{cfg.tag}: {len(days)} trading days {days[0]} -> {days[-1]}")

    trades, events, setups, revs = [], [], [], []
    for day in days:
        r = run_day(day, prov, cfg)
        trades += r["trades"]; events += r["events"]; revs += r["reversals"]
        setups += [{**asdict(s), "ref_updates": " | ".join(
            f"{t.time()}:{o}->{n}" for t, o, n in s.ref_updates),
            "notes": "; ".join(s.notes)} for s in r["setups"]]
        if not a.no_day_charts:
            report.day_chart(day, prov, r, out / "days")
        tot = sum((t["entry_price"] - t["exit_price"]) for t in r["trades"]
                  if t["exit_price"] is not None)
        print(f"  {day}: {len(r['reversals'])} reversals, {len(r['trades'])} trades, "
              f"{tot:+.2f} pts gross")

    df = pd.DataFrame(trades)
    if not df.empty:
        df["exit_signal_ts"] = pd.to_datetime(df["exit_signal_ts"])
        unpriced = df[df["exit_price"].isna()]
        if len(unpriced):
            print(f"!! {len(unpriced)} trade(s) without an exit price excluded from P&L")
        df = report.add_pnl(df[df["exit_price"].notna()], cfg)
    m, daily = report.metrics(df, days, cfg)

    df.to_csv(out / "trades.csv", index=False)
    pd.DataFrame(events).to_csv(out / "events.csv", index=False)
    pd.DataFrame(setups).to_csv(out / "setups.csv", index=False)
    pd.DataFrame(revs).to_csv(out / "reversals.csv", index=False)
    daily.rename("net_pnl").to_csv(out / "daily_pnl.csv", index_label="date")
    pd.DataFrame(prov.missing, columns=["date", "contract", "issue"]).to_csv(
        out / "data_gaps.csv", index=False)
    (out / "metrics.json").write_text(json.dumps(m, indent=2, default=float))
    (out / "config.json").write_text(json.dumps(cfg.to_dict(), indent=2))
    report.charts(df, daily, cfg, out)

    print("\n" + "=" * 60)
    for k, v in m.items():
        print(f"  {k:32s} {v:,.2f}" if isinstance(v, float) else f"  {k:32s} {v}")
    print("=" * 60)
    prov.bd.cost_report()
    print(f"Results -> {out}")


if __name__ == "__main__":
    main()
