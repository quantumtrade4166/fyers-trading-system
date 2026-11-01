import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import itertools
import pandas as pd
import run_backtest_oh_short_v3 as v3

v3.LOAD_FROM  = "2024-05-01"
v3.START_DATE = pd.Timestamp("2024-05-29")
v3.END_DATE   = "2025-12-31"

CHOSEN   = ("gap", None, "nifty_gap_down")   # picked on Jan-Jun 2026, frozen
SLIPPAGE = 0.0005                            # 0.05% round trip, sensitivity only


def show_rule(res: dict, title: str):
    tr, s = res["trades"], v3.summarise(res)
    slip = (tr["pos_val"] * SLIPPAGE).sum()
    print(f"\n{'='*70}\n  {title}\n{'='*70}")
    print(f"  Period            : {tr['date'].min()} → {tr['date'].max()}")
    print(f"  Days traded       : {s['days']}   Trades: {s['trades']}")
    print(f"  Gross P&L         : Rs {s['gross']:>+11,.0f}")
    print(f"  Costs             : Rs {s['costs']:>11,.0f}")
    print(f"  NET P&L           : Rs {s['net']:>+11,.0f}   ({s['net']/v3.STARTING_CAPITAL*100:+.1f}%)")
    print(f"  NET after 0.05% slippage : Rs {s['net']-slip:>+11,.0f}")
    print(f"  Max drawdown      : {s['max_dd']:.1f}%")
    print(f"  Win rate          : {s['win_rate']:.1f}%   PF: {s['pf']:.2f}   Avg/trade: Rs {s['avg_net']:+,.0f}")
    print(f"  Green months      : {s['green_mo']}   Max leverage: {s['max_lev']:.1f}x")

    m = tr.groupby(pd.to_datetime(tr["date"]).dt.to_period("M"))["net"].agg(["sum", "count"])
    print("\n  Month      Trades       Net P&L")
    for p, r in m.iterrows():
        print(f"  {p}   {int(r['count']):>6}   Rs {r['sum']:>+10,.0f}")
    y = tr.groupby(pd.to_datetime(tr["date"]).dt.year)["net"].sum()
    print("\n  By year: " + "   ".join(f"{k}: Rs {v:+,.0f}" for k, v in y.items()))
    print("  Exits:   " + "   ".join(f"{k} {len(g)} (Rs {g['net'].sum():+,.0f})"
                                    for k, g in tr.groupby("exit_reason")))


if __name__ == "__main__":
    sym_dirs = sorted(d for d in v3.DATA_DIR.iterdir()
                      if d.is_dir() and d.name.startswith("NSE_") and d.name.endswith("_EQ"))
    print(f"Loading {len(sym_dirs)} symbols for {v3.START_DATE.date()} → {v3.END_DATE} ...")
    all_data = {d.name: df for d in sym_dirs if (df := v3.load_5min(d)) is not None}
    flags = v3.nifty_day_flags()
    print(f"Loaded {len(all_data)} symbols.  Nifty days: {len(flags)}")

    signals = {rr: v3.scan_signals(all_data, rr) for rr in v3.TARGETS}
    print(f"Signals per target set: {len(signals[None])}")

    chosen = v3.run_portfolio(signals[None], CHOSEN[0], CHOSEN[2], v3.flags if hasattr(v3, 'flags') else flags)
    show_rule(chosen, "OUT-OF-SAMPLE: gap ranking + no target + Nifty gap-down (rule frozen)")
    chosen["trades"].to_csv(v3.OUT_DIR / "oh_short_v3_oos_trades.csv", index=False)

    rows = []
    for ranking, rr, nf in itertools.product(v3.RANKINGS, v3.TARGETS, v3.NIFTY_FILTERS):
        s = v3.summarise(v3.run_portfolio(signals[rr], ranking, nf, flags))
        if s:
            rows.append({"ranking": ranking, "target": f"1:{rr:g}" if rr else "none", "nifty": nf, **s})
    tbl = pd.DataFrame(rows).sort_values("net", ascending=False).reset_index(drop=True)
    tbl.to_csv(v3.OUT_DIR / "oh_short_v3_oos_grid.csv", index=False)

    print(f"\n{'='*108}\n  ALL 27 COMBOS ON OUT-OF-SAMPLE PERIOD (net of costs)\n{'='*108}")
    print(f"  {'#':>2} {'Ranking':<6} {'Target':<7} {'Nifty filter':<15} {'Trades':>6} {'NET':>11} "
          f"{'MaxDD':>7} {'Win%':>6} {'PF':>5} {'Avg/trd':>8} {'Green mo':>9}")
    for i, r in tbl.iterrows():
        mark = "  <-- chosen" if (r["ranking"], r["target"], r["nifty"]) == ("gap", "none", "nifty_gap_down") else ""
        print(f"  {i+1:>2} {r['ranking']:<6} {r['target']:<7} {r['nifty']:<15} {r['trades']:>6} "
              f"{r['net']:>+11,.0f} {r['max_dd']:>6.1f}% {r['win_rate']:>5.1f}% {r['pf']:>5.2f} "
              f"{r['avg_net']:>+8,.0f} {r['green_mo']:>9}{mark}")
    print(f"\n  Profitable combos: {(tbl['net'] > 0).sum()} / {len(tbl)}")
