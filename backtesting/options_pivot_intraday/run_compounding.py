"""
Compounding position sizing: lots scale with equity instead of being frozen
at 10 forever.

    lots = max(1, floor(equity / MARGIN_PER_LOT))

This makes risk grow with the account, which is what makes % drawdown
scale-invariant and therefore comparable across periods (the whole reason
we quote DD in % rather than rupees).

Runs both the original default ST(7,3) and the recommended
ST(14,2.5) + 2.0x premium stop, fixed vs compounded, plus IS/OOS.
Costs: Rs 20/order brokerage only (Rs 40/trade). No STT/slippage.
"""
import sys
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from backtesting.options_credit_spread.data_loader import OptionsDataLoader
from backtesting.options_pivot_intraday.signals import (
    build_5min_bars, add_supertrend_7_3, build_daily_pivots,
)
from backtesting.options_pivot_intraday.strategy import run_backtest

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CAPITAL = 2_000_000
MARGIN_PER_LOT = 200_000
BRK = 40

CONFIGS = {
    "ST(7,3) default":            dict(period=7,  mult=3.0, kw=dict()),
    "ST(14,2.5)+stop2.0x":        dict(period=14, mult=2.5, kw=dict(stop_mult=2.0)),
}


def apply_sizing(trades, compound: bool):
    """Walk trades in order, sizing each by the equity available at that moment."""
    df = pd.DataFrame(trades).sort_values("exit_time").reset_index(drop=True)
    eq = CAPITAL
    lots_l, pnl_l, eq_l = [], [], []
    for pnl_1lot in df["pnl_rupees"]:
        lots = max(1, int(eq // MARGIN_PER_LOT)) if compound else 10
        pnl = pnl_1lot * lots - BRK
        eq += pnl
        lots_l.append(lots); pnl_l.append(pnl); eq_l.append(eq)
    df["lots"], df["pnl_net"], df["equity"] = lots_l, pnl_l, eq_l
    df["peak"] = df["equity"].cummax()
    df["dd_r"] = df["equity"] - df["peak"]
    df["dd_p"] = df["dd_r"] / df["peak"] * 100
    return df


def stats(df, label):
    net = df["pnl_net"].sum()
    final = df["equity"].iloc[-1]
    yrs = (df["exit_time"].max() - df["exit_time"].min()).days / 365.25
    cagr = ((final / CAPITAL) ** (1 / yrs) - 1) * 100
    wins = df.loc[df.pnl_net > 0, "pnl_net"].sum()
    loss = df.loc[df.pnl_net < 0, "pnl_net"].sum()
    return {
        "run": label, "trades": len(df), "final_L": round(final / 100000, 2),
        "net_L": round(net / 100000, 2), "CAGR%": round(cagr, 2),
        "maxDD%": round(df.dd_p.min(), 2), "maxDD_L": round(df.dd_r.min() / 100000, 2),
        "PF": round(wins / abs(loss), 2), "win%": round((df.pnl_net > 0).mean() * 100, 1),
        "endLots": int(df["lots"].iloc[-1]), "worst_L": round(df.pnl_net.min() / 100000, 2),
    }


def main():
    loader = OptionsDataLoader()
    spot = loader.spot_1min_series()
    base = build_5min_bars(spot)
    pivots = build_daily_pivots(spot)

    all_rows, keep = [], {}
    for name, cfg in CONFIGS.items():
        p, m = cfg["period"], cfg["mult"]
        bars = add_supertrend_7_3(base.copy(), period=p, multiplier=m)
        dcol, scol = f"supertrend_dir_{p}_{m}", f"supertrend_{p}_{m}"

        splits = [("FULL", None, None), ("IS 21-23", "2021-01-01", "2023-12-31"),
                  ("OOS 24-26", "2024-01-01", "2026-12-31")]
        for split, d0, d1 in splits:
            t = run_backtest(loader, bars, pivots, dcol, scol, date_from=d0, date_to=d1, **cfg["kw"])
            for mode in ("fixed 10", "compound"):
                df = apply_sizing(t, compound=(mode == "compound"))
                all_rows.append({"config": name, "split": split, "sizing": mode,
                                 **stats(df, f"{name} | {split} | {mode}")})
                if split == "FULL":
                    keep[(name, mode)] = df

    out = pd.DataFrame(all_rows)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(RESULTS_DIR / "compounding.csv", index=False)

    cols = ["config", "split", "sizing", "trades", "final_L", "net_L", "CAGR%", "maxDD%", "maxDD_L", "PF", "endLots"]
    print("=" * 130)
    print("FIXED vs COMPOUNDING  —  Rs 20L start, Rs 2L margin/lot, Rs 20/order")
    print("=" * 130)
    print(out[cols].to_string(index=False))

    print("\n" + "=" * 90)
    print("THE POINT: does % DD become comparable across periods once we compound?")
    print("=" * 90)
    for name in CONFIGS:
        print(f"\n{name}")
        for mode in ("fixed 10", "compound"):
            sub = out[(out.config == name) & (out.sizing == mode)]
            vals = {r["split"]: r["maxDD%"] for _, r in sub.iterrows()}
            spread = abs(vals["FULL"] - vals["OOS 24-26"])
            print(f"  {mode:9s}  FULL {vals['FULL']:7.2f}%   IS {vals['IS 21-23']:7.2f}%   "
                  f"OOS {vals['OOS 24-26']:7.2f}%   |FULL-OOS| = {spread:.2f}pp")

    # yearly, compounded, recommended config
    df = keep[("ST(14,2.5)+stop2.0x", "compound")]
    df["yr"] = df["exit_time"].dt.year
    print("\n" + "=" * 90)
    print("YEARLY — ST(14,2.5)+stop2.0x, COMPOUNDED")
    print("=" * 90)
    print("Year  Trades  Lots(avg)   Abs P&L      % on yr-start   MaxDD%   Year-end equity")
    prev = CAPITAL
    for y, g in df.groupby("yr"):
        pnl = g["pnl_net"].sum(); end = g["equity"].iloc[-1]
        print("%-6d%7d%11.1f%13s%15.1f%%%9.2f%%%18s" % (
            y, len(g), g["lots"].mean(), format(int(pnl), ","), pnl / prev * 100,
            g["dd_p"].min(), format(int(end), ",")))
        prev = end
    df.to_csv(RESULTS_DIR / "trades_compounded_best.csv", index=False)
    print(f"\nSaved: {RESULTS_DIR/'compounding.csv'}")


if __name__ == "__main__":
    main()
