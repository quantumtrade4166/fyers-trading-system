import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
from pathlib import Path
from strategy_55day_breakout import (
    load_symbol, compute_signals, DATA_DIR,
    STARTING_CAPITAL, MAX_POSITIONS, POSITION_SIZE
)

TRADE_STOP_PCT  = -0.15   # exit individual position if PNL <= -15% from entry
NIFTY_MA_PERIOD = 200

def load_nifty(path: Path) -> pd.Series:
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    ma = df["close"].rolling(NIFTY_MA_PERIOD).mean()
    return df["close"] > ma   # True = Nifty above 200MA


def run_v6(all_data: dict, all_dates: list,
           use_nifty_filter: bool = False,
           nifty_above: pd.Series = None) -> dict:

    open_positions: dict[str, dict] = {}
    trades: list[dict] = []
    daily_equity: list[dict] = []
    cash = STARTING_CAPITAL

    for date in all_dates:
        # ── Exits: trail stop OR -15% hard stop ───────────────
        to_exit = []
        for sym, pos in open_positions.items():
            df = all_data.get(sym)
            if df is None or date not in df.index:
                continue
            row = df.loc[date]
            current_price = row["close"]
            pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"]

            if row["exit_signal"] or pnl_pct <= TRADE_STOP_PCT:
                reason = "hard_stop" if pnl_pct <= TRADE_STOP_PCT else "trail_stop"
                to_exit.append((sym, reason))

        for sym, reason in to_exit:
            pos = open_positions.pop(sym)
            df = all_data[sym]
            sym_dates = df.index.tolist()
            idx = sym_dates.index(date)
            exit_price = df.iloc[idx + 1]["open"] if idx + 1 < len(sym_dates) else df.loc[date]["close"]
            proceeds = pos["shares"] * exit_price
            cash += proceeds
            pnl = proceeds - pos["shares"] * pos["entry_price"]
            trades.append({
                "symbol":      sym,
                "entry_date":  pos["entry_date"],
                "exit_date":   date,
                "entry_price": pos["entry_price"],
                "exit_price":  exit_price,
                "shares":      pos["shares"],
                "pnl":         pnl,
                "return_pct":  pnl / (pos["shares"] * pos["entry_price"]) * 100,
                "exit_reason": reason,
            })

        # ── Entries ───────────────────────────────────────────
        market_ok = True
        if use_nifty_filter and nifty_above is not None:
            market_ok = bool(nifty_above.get(date, False))

        if market_ok:
            slots_free = MAX_POSITIONS - len(open_positions)
            if slots_free > 0:
                candidates = []
                for sym, df in all_data.items():
                    if sym in open_positions or date not in df.index:
                        continue
                    if df.loc[date]["entry_signal"]:
                        candidates.append((sym, df, date))

                for sym, df, sig_date in candidates[:slots_free]:
                    sym_dates = df.index.tolist()
                    idx = sym_dates.index(sig_date)
                    if idx + 1 >= len(sym_dates):
                        continue
                    entry_price = df.iloc[idx + 1]["open"]
                    if entry_price <= 0:
                        continue
                    shares = int(POSITION_SIZE / entry_price)
                    if shares == 0 or shares * entry_price > cash:
                        continue
                    cash -= shares * entry_price
                    open_positions[sym] = {
                        "shares":      shares,
                        "entry_price": entry_price,
                        "entry_date":  df.index[idx + 1],
                    }

        # ── Mark-to-market ────────────────────────────────────
        open_value = sum(
            pos["shares"] * all_data[sym].loc[date]["close"]
            for sym, pos in open_positions.items()
            if sym in all_data and date in all_data[sym].index
        )
        daily_equity.append({"date": date, "equity": cash + open_value,
                              "open_positions": len(open_positions)})

    # Close remaining at last close
    for sym, pos in open_positions.items():
        df = all_data[sym]
        last_price = df["close"].iloc[-1]
        proceeds = pos["shares"] * last_price
        cash += proceeds
        pnl = proceeds - pos["shares"] * pos["entry_price"]
        trades.append({
            "symbol": sym, "entry_date": pos["entry_date"],
            "exit_date": df.index[-1], "entry_price": pos["entry_price"],
            "exit_price": last_price, "shares": pos["shares"],
            "pnl": pnl,
            "return_pct": pnl / (pos["shares"] * pos["entry_price"]) * 100,
            "exit_reason": "eod",
        })

    eq_df = pd.DataFrame(daily_equity).set_index("date")
    tr_df = pd.DataFrame(trades) if trades else pd.DataFrame()
    return {"equity": eq_df, "trades": tr_df}


def print_summary(label: str, result: dict):
    eq = result["equity"]["equity"]
    tr = result["trades"]

    final  = eq.iloc[-1]
    peak   = eq.cummax()
    dd     = (eq - peak) / peak * 100
    max_dd = dd.min()
    years  = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr   = ((final / STARTING_CAPITAL) ** (1 / years) - 1) * 100

    wins   = tr[tr["pnl"] > 0]
    losses = tr[tr["pnl"] <= 0]
    wr     = len(wins) / len(tr) * 100 if len(tr) else 0
    pf     = wins["pnl"].sum() / abs(losses["pnl"].sum()) if len(losses) else 0

    if "exit_reason" in tr.columns:
        hard_stops = (tr["exit_reason"] == "hard_stop").sum()
        trail_stops = (tr["exit_reason"] == "trail_stop").sum()
    else:
        hard_stops = trail_stops = 0

    print(f"\n{'='*62}")
    print(f"  {label}")
    print(f"{'='*62}")
    print(f"  Final Equity   : ₹{final:>12,.0f}")
    print(f"  Net P&L        : ₹{final - STARTING_CAPITAL:>+12,.0f}")
    print(f"  CAGR           : {cagr:>7.2f}%")
    print(f"  Max Drawdown   : {max_dd:>7.2f}%")
    print(f"  Total Trades   : {len(tr):>6}")
    print(f"  Win Rate       : {wr:>7.1f}%")
    print(f"  Profit Factor  : {pf:>7.2f}")
    print(f"  Avg Positions  : {result['equity']['open_positions'].mean():>7.1f}")
    print(f"  Hard Stops Hit : {hard_stops:>6}  ({hard_stops/len(tr)*100:.1f}%)")
    print(f"  Trail Stops    : {trail_stops:>6}  ({trail_stops/len(tr)*100:.1f}%)")

    print(f"\n  {'Year':<6} {'Start':>12} {'End':>12} {'P&L':>12} {'Ret':>7}")
    print("  " + "-"*52)
    for yr, grp in result["equity"]["equity"].resample("YE"):
        s = grp.iloc[0]; e = grp.iloc[-1]
        p = e - s; r = p / s * 100
        print(f"  {yr.year:<6} ₹{s:>10,.0f} ₹{e:>10,.0f} ₹{p:>+10,.0f} {r:>+6.1f}%")
    print()


if __name__ == "__main__":
    symbols = [p.stem for p in DATA_DIR.glob("*.parquet")]
    print(f"Loading {len(symbols)} symbols...")

    all_data = {}
    for sym in symbols:
        df = load_symbol(sym)
        if df is None:
            continue
        df = compute_signals(df)
        if len(df) >= 10:
            all_data[sym] = df

    all_dates = sorted(set().union(*[set(df.index) for df in all_data.values()]))
    print(f"Symbols: {len(all_data)} | Dates: {len(all_dates)}")

    nifty_above = load_nifty(Path(__file__).parent / "nifty_daily.parquet")

    print("\nRunning V6a — Hard Stop −15% only...")
    r6a = run_v6(all_data, all_dates, use_nifty_filter=False)

    print("Running V6b — Hard Stop −15% + Nifty 200MA filter...")
    r6b = run_v6(all_data, all_dates, use_nifty_filter=True, nifty_above=nifty_above)

    print_summary("V6a — Hard Stop −15% per trade  (no market filter)", r6a)
    print_summary("V6b — Hard Stop −15% + Nifty 200MA filter",          r6b)

    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    for tag, r in [("v6a", r6a), ("v6b", r6b)]:
        r["trades"].to_csv(out / f"trades_55day_{tag}.csv", index=False)
        r["equity"].to_csv(out / f"equity_55day_{tag}.csv")

    # ── Final comparison table ────────────────────────────────
    print("\n" + "="*72)
    print("  FULL COMPARISON — V1 through V6b")
    print("="*72)
    print(f"  {'Version':<42} {'CAGR':>7} {'Max DD':>9} {'PF':>6}")
    print("  " + "-"*68)

    prev = {
        "V1  No filter":                   "equity_55day_v1.csv",
        "V2  Nifty 200MA block entries":   "equity_55day_v2.csv",
        "V3  Yearly cap 15%":              "equity_55day_v3.csv",
        "V4  Hard Nifty exit":             "equity_55day_v4.csv",
        "V5  Combined cap+exit":           "equity_55day_v5.csv",
    }
    for name, csv in prev.items():
        eq = pd.read_csv(out / csv, index_col=0, parse_dates=True)["equity"]
        tr = pd.read_csv(out / csv.replace("equity_", "trades_"))
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        cagr  = ((eq.iloc[-1] / STARTING_CAPITAL) ** (1/years) - 1) * 100
        pk    = eq.cummax(); mdd = ((eq-pk)/pk*100).min()
        w = tr[tr["pnl"]>0]; l = tr[tr["pnl"]<=0]
        pf = w["pnl"].sum()/abs(l["pnl"].sum()) if len(l) else 0
        print(f"  {name:<42} {cagr:>6.2f}% {mdd:>8.2f}%  {pf:>5.2f}")

    for name, r in [("V6a Hard stop −15% only", r6a), ("V6b Hard stop −15% + Nifty filter", r6b)]:
        eq = r["equity"]["equity"]; tr = r["trades"]
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        cagr  = ((eq.iloc[-1] / STARTING_CAPITAL) ** (1/years) - 1) * 100
        pk    = eq.cummax(); mdd = ((eq-pk)/pk*100).min()
        w = tr[tr["pnl"]>0]; l = tr[tr["pnl"]<=0]
        pf = w["pnl"].sum()/abs(l["pnl"].sum()) if len(l) else 0
        print(f"  {name:<42} {cagr:>6.2f}% {mdd:>8.2f}%  {pf:>5.2f}")
    print()
