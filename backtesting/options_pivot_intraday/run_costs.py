"""
Full transaction-cost backtest for the recommended setup:
ST(14, 2.5) + 2x premium stop, naked ATM selling, NIFTY weekly.

Re-prices every trade with statutory charges + slippage, then re-runs the
sizing loop so costs feed back into equity (and so into the lot count when
compounding). Slippage is modelled on the fills:
    sell fill = entry_price - slip       (you sell a bit lower)
    buy  fill = exit_price  + slip       (you buy back a bit higher)
Premium-stop exits get 2x slippage (stops fill in fast markets).
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from backtesting.options_pivot_intraday.costs import statutory_costs, BROKERAGE_PER_ORDER

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CAPITAL = 2_000_000
MARGIN_PER_LOT = 200_000
QTY_PER_LOT = 75
STOP_SLIP_MULT = 2.0

SCENARIOS = [
    # label, statutory?, slippage function (lots -> points per side)
    ("brokerage only (old model)", False, lambda lots: 0.0),
    ("all charges, no slippage",   True,  lambda lots: 0.0),
    ("all charges + 0.25pt slip",  True,  lambda lots: 0.25),
    ("all charges + 0.50pt slip",  True,  lambda lots: 0.50),
    ("all charges + 1.00pt slip",  True,  lambda lots: 1.00),
    ("all charges + size-scaled",  True,  lambda lots: 0.25 + 0.01 * lots),
]
REALISTIC = "all charges + size-scaled"


def simulate(trades, compound, statutory, slip_fn):
    eq = CAPITAL
    rows = []
    for t in trades.itertuples(index=False):
        lots = max(1, int(eq // MARGIN_PER_LOT)) if compound else 10
        qty = lots * QTY_PER_LOT
        s_in = slip_fn(lots)
        s_out = s_in * (STOP_SLIP_MULT if t.exit_reason == "premium_stop" else 1.0)
        sell_fill = max(0.05, t.entry_price - s_in)
        buy_fill = t.exit_price + s_out
        gross = (sell_fill - buy_fill) * qty
        if statutory:
            c = statutory_costs(sell_fill, buy_fill, qty, t.date)
        else:
            flat = BROKERAGE_PER_ORDER * 2
            c = {"brokerage": flat, "stt": 0.0, "exch": 0.0, "sebi": 0.0,
                 "stamp": 0.0, "gst": 0.0, "total": flat}
        net = gross - c["total"]
        eq += net
        rows.append({"exit_time": t.exit_time, "date": t.date, "lots": lots,
                     "gross_noslip": (t.entry_price - t.exit_price) * qty,
                     "slippage": (s_in + s_out) * qty, "net": net, "equity": eq, **c})
    df = pd.DataFrame(rows)
    df["peak"] = df["equity"].cummax()
    df["dd_p"] = (df["equity"] - df["peak"]) / df["peak"] * 100
    return df


def stats(df):
    net = df["net"].sum()
    yrs = (df["exit_time"].max() - df["exit_time"].min()).days / 365.25
    final = CAPITAL + net
    cagr = ((final / CAPITAL) ** (1 / yrs) - 1) * 100 if final > 0 else float("nan")
    wins = df.loc[df.net > 0, "net"].sum()
    loss = df.loc[df.net < 0, "net"].sum()
    gross = df["gross_noslip"].sum()
    cost = df["total"].sum() + df["slippage"].sum()
    return {
        "trades": len(df), "win%": round((df.net > 0).mean() * 100, 1),
        "PF": round(wins / abs(loss), 2) if loss else None,
        "net_L": round(net / 1e5, 2), "CAGR%": round(cagr, 2),
        "maxDD%": round(df.dd_p.min(), 2),
        "gross_L": round(gross / 1e5, 2), "costs_L": round(cost / 1e5, 2),
        "cost_pct_gross": round(cost / gross * 100, 1) if gross else None,
        "cost_per_trade": round(cost / len(df)), "endLots": int(df["lots"].iloc[-1]),
    }


def cost_breakdown(df, title):
    g = df["gross_noslip"].sum()
    print(f"\nCOST BREAKDOWN - {title}  (gross before costs Rs {g / 1e5:.2f}L)")
    for k in ["slippage", "stt", "exch", "gst", "brokerage", "stamp", "sebi"]:
        v = df[k].sum()
        print(f"  {k:10s} Rs {v / 1e5:8.2f}L   {v / g * 100:5.1f}% of gross   Rs {v / len(df):7,.0f}/trade")
    tot = df["slippage"].sum() + df["total"].sum()
    print(f"  {'TOTAL':10s} Rs {tot / 1e5:8.2f}L   {tot / g * 100:5.1f}% of gross   Rs {tot / len(df):7,.0f}/trade")


def main():
    trades = pd.read_csv(RESULTS_DIR / "trades_compounded_best.csv",
                         parse_dates=["entry_time", "exit_time"])
    trades = trades.sort_values("exit_time").reset_index(drop=True)
    trades["date"] = trades["date"].astype(str)
    print(f"ST(14,2.5)+2x stop | {len(trades):,} trades | "
          f"{trades['date'].min()} to {trades['date'].max()}")
    print(f"Avg entry premium Rs {trades['entry_price'].mean():.1f} | "
          f"premium-stop exits: {(trades['exit_reason'] == 'premium_stop').sum()}\n")

    rows, keep = [], {}
    for mode in ("fixed 10", "compound"):
        for label, statutory, fn in SCENARIOS:
            df = simulate(trades, mode == "compound", statutory, fn)
            rows.append({"sizing": mode, "scenario": label, **stats(df)})
            keep[(mode, label)] = df
    out = pd.DataFrame(rows)
    out.to_csv(RESULTS_DIR / "cost_scenarios.csv", index=False)

    print("=" * 128)
    print("COST SCENARIOS - Rs 20L start, Rs 2L margin/lot, 75 qty/lot, NIFTY freeze 1800")
    print("=" * 128)
    print(out.to_string(index=False))

    for mode in ("fixed 10", "compound"):
        cost_breakdown(keep[(mode, REALISTIC)], f"{mode}, {REALISTIC}")

    # break-even slippage, fixed 10 lots, all statutory charges
    lo, hi = 0.0, 20.0
    for _ in range(40):
        mid = (lo + hi) / 2
        n = simulate(trades, False, True, lambda lots, m=mid: m)["net"].sum()
        lo, hi = (mid, hi) if n > 0 else (lo, mid)
    print(f"\nBREAK-EVEN SLIPPAGE (fixed 10 lots, all charges): {lo:.2f} points per side "
          f"(stop exits {lo * STOP_SLIP_MULT:.2f})")

    print(f"\nIS / OOS - {REALISTIC}")
    fn = {l: f for l, _, f in SCENARIOS}[REALISTIC]
    for name, d0, d1 in [("IS 2021-23", "2021-01-01", "2023-12-31"),
                         ("OOS 2024-26", "2024-01-01", "2026-12-31")]:
        sub = trades[(trades["date"] >= d0) & (trades["date"] <= d1)]
        for mode in ("fixed 10", "compound"):
            s = stats(simulate(sub, mode == "compound", True, fn))
            print(f"  {name:12s} {mode:9s} trades={s['trades']:5d} PF={s['PF']}  net=Rs{s['net_L']:7.2f}L  "
                  f"CAGR={s['CAGR%']:6.2f}%  DD={s['maxDD%']:7.2f}%  costs={s['cost_pct_gross']}% of gross")

    df = keep[("compound", REALISTIC)].copy()
    df["yr"] = pd.to_datetime(df["exit_time"]).dt.year
    print(f"\nYEARLY - compounded, {REALISTIC}")
    print("Year  Trades  AvgLots   Net P&L      %yr-start  MaxDD%    Costs     Year-end equity")
    prev = CAPITAL
    for y, g in df.groupby("yr"):
        pnl = g["net"].sum()
        end = g["equity"].iloc[-1]
        cost = g["total"].sum() + g["slippage"].sum()
        print("%-6d%7d%9.1f%14s%10.1f%%%8.2f%%%11s%18s" % (
            y, len(g), g["lots"].mean(), format(int(pnl), ","), pnl / prev * 100,
            g["dd_p"].min(), format(int(cost), ","), format(int(end), ",")))
        prev = end

    keep[("compound", REALISTIC)].to_csv(RESULTS_DIR / "trades_with_costs_compound.csv", index=False)
    keep[("fixed 10", REALISTIC)].to_csv(RESULTS_DIR / "trades_with_costs_fixed.csv", index=False)
    print(f"\nSaved: {RESULTS_DIR / 'cost_scenarios.csv'}")


if __name__ == "__main__":
    main()
