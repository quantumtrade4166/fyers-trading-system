"""Nifty ST Directional rules on Delta BTC daily options -- full-cycle session.

    .venv\\Scripts\\python.exe -m backtesting.options_st_directional.run_btc --start 2024-01-01 --end 2026-12-31

Same DayMachine as NIFTY (spot ST(10,3) bias, option ref-low entry, option-ST exit,
one position, flips create new setups), on BTC: session D-1 17:30 -> D 17:10 IST,
contract settling D, 1000 contracts (1 BTC). See btc_provider.py for the mapping.

Costs (USD): Delta fee per side = min(0.01% of spot notional, 3.5% of premium);
half the bid/ask spread per side from config/spread_model.json (measured on our
live chain archive; BTC_SPREAD=p75 for the wide-market stress case). Days whose
marks break no-arbitrage (bad_marks > 0.2%) are skipped, as in the VWAP backtest.

Outputs -> results/BTC_{tag}/ trades.csv, daily_pnl.csv, monthly.csv, metrics.json, equity.png
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from backtesting.options_st_directional.config import Config
from backtesting.options_st_directional.engine import DayMachine
from backtesting.options_st_directional.btc_provider import BtcProvider, ROOT

sys.path.insert(0, str(ROOT / "live_trading_options" / "delta_btc"))
from backtest.vwap_history import spread_for, fee_for, bad_marks, load_day  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"


def run_day(day, prov, cfg):
    m = DayMachine(day, prov, cfg)
    m.first, m.sq = prov.session(day)
    spot = prov.spot_candles(day)
    if spot.empty or m.first not in spot.index:
        m.log(m.first, "no_spot_data")
        return m.result()
    for t in spot.index:
        if t + m.step > m.sq or not m.on_close(t):
            break
    m.square_off()
    return m.result()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--contracts", type=int, default=1000)
    ap.add_argument("--capital", type=float, default=20_000.0, help="USD, for DD %% only")
    ap.add_argument("--tag", default="FULLCYCLE")
    a = ap.parse_args()
    cfg = Config(start=a.start, end=a.end, tag=a.tag)
    cv = 0.001
    qty = a.contracts * cv

    prov = BtcProvider(cfg, a.start, a.end)
    out = RESULTS / f"BTC_{a.tag}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"BTC {a.tag}: {len(prov.days)} settlement days {prov.days[0]} -> {prov.days[-1]}")

    trades, skipped, traded_days = [], [], []
    for day in prov.days:
        chk = load_day(day)
        if chk is None or bad_marks(chk) > 0.002:
            skipped.append(day)
            continue
        r = run_day(day, prov, cfg)
        traded_days.append(day)
        for t in r["trades"]:
            if t["exit_price"] is None:
                continue
            sp_in, sp_out = prov.spot_at(t["entry_ts"]), prov.spot_at(t["exit_ts"])
            t["points"] = t["entry_price"] - t["exit_price"]
            t["gross_usd"] = t["points"] * qty
            t["spread_usd"] = (spread_for(t["entry_price"]) + spread_for(t["exit_price"])) / 2 * qty
            t["fees_usd"] = fee_for(t["entry_price"], a.contracts, sp_in) + fee_for(t["exit_price"], a.contracts, sp_out)
            t["net_usd"] = t["gross_usd"] - t["spread_usd"] - t["fees_usd"]
            trades.append(t)
        day_net = sum(t["net_usd"] for t in r["trades"] if t.get("net_usd") is not None)
        print(f"  {day}: {len(r['reversals'])} flips, {len(r['trades'])} trades, net ${day_net:+.2f}")

    df = pd.DataFrame(trades)
    df.to_csv(out / "trades.csv", index=False)
    daily = (df.groupby(df["date"])["net_usd"].sum() if not df.empty else pd.Series(dtype=float))
    daily = daily.reindex(traded_days, fill_value=0.0)
    daily.rename("net_usd").to_csv(out / "daily_pnl.csv", index_label="date")

    eq = daily.cumsum()
    dd = eq - eq.cummax()
    mon = df.assign(month=df["date"].str[:7]).groupby("month").agg(
        trades=("net_usd", "size"), gross=("gross_usd", "sum"), costs=("fees_usd", "sum"),
        spread=("spread_usd", "sum"), net=("net_usd", "sum"))
    mon.round(2).to_csv(out / "monthly.csv")
    wins, losses = df[df.net_usd > 0].net_usd, df[df.net_usd <= 0].net_usd
    m = {
        "days": len(traded_days), "days_skipped_bad_marks": len(skipped),
        "trades": len(df), "win_rate_pct": round(100 * len(wins) / max(len(df), 1), 1),
        "gross_usd": round(df.gross_usd.sum(), 2), "fees_usd": round(df.fees_usd.sum(), 2),
        "spread_usd": round(df.spread_usd.sum(), 2), "net_usd": round(df.net_usd.sum(), 2),
        "profit_factor": round(wins.sum() / -losses.sum(), 2) if losses.sum() else None,
        "avg_trade_net": round(df.net_usd.mean(), 2),
        "max_dd_usd": round(dd.min(), 2), "max_dd_date": str(dd.idxmin()),
        "sharpe": round(daily.mean() / daily.std() * np.sqrt(365), 2) if daily.std() else None,
        "exit_reasons": df.exit_reason.value_counts().to_dict(),
        "by_year_net": df.groupby(df.date.str[:4]).net_usd.sum().round(2).to_dict(),
        "by_year_gross": df.groupby(df.date.str[:4]).gross_usd.sum().round(2).to_dict(),
        "contracts": a.contracts, "spread_mode": __import__("os").environ.get("BTC_SPREAD", "median"),
    }
    (out / "metrics.json").write_text(json.dumps(m, indent=2, default=str))

    fig, ax = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    x = pd.to_datetime(eq.index)
    ax[0].plot(x, eq.values); ax[0].set_title(f"BTC ST Directional full cycle — net USD ({a.contracts} contracts)")
    ax[1].fill_between(x, dd.values, 0, color="red", alpha=.4); ax[1].set_title("Drawdown USD")
    fig.tight_layout(); fig.savefig(out / "equity.png", dpi=110)

    print("\n" + json.dumps(m, indent=2, default=str))
    print(mon.round(0).to_string())
    print(f"Results -> {out}")


if __name__ == "__main__":
    main()
