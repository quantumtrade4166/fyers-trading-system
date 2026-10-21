import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
11.9 on BTC 4h — the in-sample-chosen config (both sides, rv_min 1.5,
min_bars 10, max_h 5) on a $10,000 account, 1% risk per trade (compounding,
no leverage). Year-by-year P&L in $ and %, vs BTC buy & hold.

    .venv\\Scripts\\python.exe -m backtesting.book_strategies.coulling.run_btc_11_9_capital
"""

from pathlib import Path

import pandas as pd

from backtesting.engine import harness, vpa
from backtesting.engine.portfolio import simulate, stats
from backtesting.engine.universe import combine_sides
from backtesting.book_strategies.coulling.strategies import s11_9
from backtesting.book_strategies.coulling.run_btc_11_9 import H
from backtesting.book_strategies.kaufman.data_btc import get_bars

CAP, OOS = 10_000.0, "2022-01-01"
CFG = {"rv_min": 1.5, "min_bars": 10, "max_h": 5.0, "max_age": 120, "sides": "both"}
OUT = Path(__file__).resolve().parent / "results"

if __name__ == "__main__":
    df = get_bars("4h")
    f, fm = vpa.add_vpa_features(df, False), vpa.add_vpa_features(vpa.mirror(df), False)
    sig = combine_sides(s11_9, f, fm, CFG)
    tr = harness.run(f, sig, {**H, "capital": CAP})
    t = pd.DataFrame([x.to_dict() for x in tr])
    t["R"] = t["pnl"] / (CAP * 0.01); t["R_gross"] = t["gross_pnl"] / (CAP * 0.01)
    acct, eq = simulate(t, CAP, calendar="calendar")
    acct.to_csv(OUT / "btc_11_9_4h_10k_trades.csv", index=False)

    a = acct[acct["taken"]].copy()
    a["year"] = a["exit_time"].dt.year
    btc = get_bars("1D")["close"]
    rows = []
    for y, g in a.groupby("year"):
        e = eq[eq.index.year == y]
        start = eq[eq.index < e.index[0]].iloc[-1] if (eq.index < e.index[0]).any() else CAP
        b = btc[btc.index.year == y]
        rows.append({"year": y, "trades": len(g), "wins": int((g["pnl_acct"] > 0).sum()),
                     "start_$": round(start), "pnl_$": round(g["pnl_acct"].sum()),
                     "end_$": round(e.iloc[-1]),
                     "return_%": round((e.iloc[-1] / start - 1) * 100, 1),
                     "worst_dd_%": round(((e / e.cummax().clip(lower=start)) - 1).min() * 100, 1),
                     "btc_hold_%": round((b.iloc[-1] / b.iloc[0] - 1) * 100, 1)})
    yt = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(yt.to_string(index=False))
    for name, kw in (("FULL 2017-08 → 2026-09", {}), ("TUNING 2017-08 → 2021", {"end": OOS}),
                     ("TEST 2022 → 2026-09", {"start": OOS})):
        s = stats(eq, acct, calendar="calendar", **kw)
        e = eq if not kw else (eq[eq.index < OOS] if "end" in kw else eq[eq.index >= OOS])
        peak = e.cummax(); dd_usd = (e - peak).min()
        print(f"\n{name}: start ${e.iloc[0]:,.0f} → end ${e.iloc[-1]:,.0f}  "
              f"(P&L ${e.iloc[-1]-e.iloc[0]:+,.0f}, {(e.iloc[-1]/e.iloc[0]-1)*100:+.1f}%)  "
              f"CAGR {s['cagr_pct']}%  max DD {s['maxdd_pct']}% (${dd_usd:,.0f})  "
              f"trades {s['trades']}  win {s['win_pct']}%  PF {s['pf']}  Sharpe {s['sharpe']}")
    print(f"\nLongs: {int((a.direction=='long').sum())} trades ${a.loc[a.direction=='long','pnl_acct'].sum():+,.0f}"
          f" | Shorts: {int((a.direction=='short').sum())} trades ${a.loc[a.direction=='short','pnl_acct'].sum():+,.0f}"
          f" | costs+funding paid ${(a['cost']*a['scale']).sum():,.0f}")
    print(f"Avg position size: {(a.entry_price*a.units*a.scale).mean():,.0f} $ "
          f"({(a.entry_price*a.units*a.scale/ (CAP)).mean()*100:.0f}% of starting capital); "
          f"avg hold {a.bars_held.mean()*4/24:.1f} days; best ${a.pnl_acct.max():+,.0f}, worst ${a.pnl_acct.min():+,.0f}")
    big = a["pnl_acct"].idxmax()
    t2 = t.drop(index=big)
    ac2, eq2 = simulate(t2, CAP, calendar="calendar")
    for name, kw in (("WITHOUT best trade — FULL", {}), ("WITHOUT best trade — TUNING", {"end": OOS}),
                     ("WITHOUT best trade — TEST", {"start": OOS})):
        s2 = stats(eq2, ac2, calendar="calendar", **kw)
        print(f"{name}: CAGR {s2['cagr_pct']}%  max DD {s2['maxdd_pct']}%  end ${eq2.iloc[-1]:,.0f}" if not kw else
              f"{name}: CAGR {s2['cagr_pct']}%  max DD {s2['maxdd_pct']}%")
    print(f"(best trade: {a.loc[big,'entry_time']} {a.loc[big,'direction']} ${a.loc[big,'pnl_acct']:+,.0f}, "
          f"position {a.loc[big,'entry_price']*a.loc[big,'units']*a.loc[big,'scale']/a.loc[big,'scale']/CAP*100:.0f}% of base capital)")
    hold = btc[btc.index >= a.entry_time.min().normalize()]
    print(f"BTC buy & hold of $10,000 from first trade: ${CAP*hold.iloc[-1]/hold.iloc[0]:,.0f}")
