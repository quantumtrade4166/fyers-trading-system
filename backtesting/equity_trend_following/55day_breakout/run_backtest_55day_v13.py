import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path
from strategy_55day_breakout import (
    load_symbol, compute_signals, DATA_DIR,
    STARTING_CAPITAL, MAX_POSITIONS
)

POSITION_PCT      = 0.01      # 1% of current equity per trade (compounding)
MAX_POSITION_CAP  = 200_000   # ₹2L cap per position
HWM_DD_STOP       = 0.15      # exit ALL when equity falls 15% below high-watermark
NIFTY_200MA       = 200       # daily entry block
NIFTY_100MA       = 100       # monthly exit/entry signal


def load_nifty(path: Path):
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)

    # Daily 200MA — blocks new entries each day
    ma200 = df["close"].rolling(NIFTY_200MA).mean()
    above_200 = df["close"] > ma200

    # Monthly 100MA — force-exit all when month-end close < 100MA
    ma100 = df["close"].rolling(NIFTY_100MA).mean()
    monthly = df["close"].resample("ME").last()
    ma100_monthly = ma100.resample("ME").last()
    above_100_monthly = (monthly > ma100_monthly)   # True = market OK, False = exit all

    return above_200, above_100_monthly


def get_hwm_exit_price(df: pd.DataFrame, date) -> float:
    """HWM stop — exit at today's close."""
    if date in df.index:
        return df.loc[date]["close"]
    idx = df.index.searchsorted(date)
    if idx < len(df):
        return df.iloc[idx]["close"]
    return df.iloc[-1]["close"]


def get_trail_exit_price(df: pd.DataFrame, date) -> float:
    """Trail stop — next trading day open."""
    idx = df.index.searchsorted(date)
    if idx < len(df) and df.index[idx] == date:
        next_idx = idx + 1
        return df.iloc[next_idx]["open"] if next_idx < len(df) else df.iloc[idx]["close"]
    elif idx < len(df):
        return df.iloc[idx]["open"]
    return df.iloc[-1]["close"]


def run_v13(all_data: dict, all_dates: list,
            above_200: pd.Series,
            above_100_monthly: pd.Series,
            use_200ma: bool = True,
            use_100ma_monthly: bool = True) -> dict:

    open_positions: dict[str, dict] = {}
    trades:        list[dict] = []
    daily_equity:  list[dict] = []
    cash = STARTING_CAPITAL

    peak_equity       = STARTING_CAPITAL
    stop_events       = []
    monthly_exits     = []
    market_regime_ok  = True   # tracks monthly 100MA state

    prev_month = None

    for date in all_dates:

        # ── Monthly 100MA check (at month boundary) ────────────────────
        this_month = (date.year, date.month)
        if this_month != prev_month:
            prev_month = this_month
            # Use last month's signal (month-end close of previous month)
            prev_month_end = date - pd.offsets.MonthBegin(1)
            # Find the most recent monthly signal at or before this date
            monthly_signals = above_100_monthly[above_100_monthly.index <= date]
            if use_100ma_monthly and len(monthly_signals) > 0:
                new_regime = bool(monthly_signals.iloc[-1])
                if not new_regime and market_regime_ok and open_positions:
                    # Regime turned bearish — force exit ALL at today's close
                    monthly_exits.append({
                        "date":             date,
                        "reason":           "nifty_100ma_exit",
                        "positions_closed": len(open_positions),
                        "equity_before":    cash + sum(
                            pos["shares"] * all_data[sym].loc[date]["close"]
                            for sym, pos in open_positions.items()
                            if sym in all_data and date in all_data[sym].index
                        ),
                    })
                    for sym in list(open_positions.keys()):
                        pos        = open_positions.pop(sym)
                        exit_price = get_hwm_exit_price(all_data[sym], date)
                        proceeds   = pos["shares"] * exit_price
                        cash      += proceeds
                        pnl        = proceeds - pos["shares"] * pos["entry_price"]
                        trades.append({
                            "symbol":        sym,
                            "entry_date":    pos["entry_date"],
                            "exit_date":     date,
                            "entry_price":   pos["entry_price"],
                            "exit_price":    exit_price,
                            "shares":        pos["shares"],
                            "position_size": pos["position_size"],
                            "pnl":           pnl,
                            "return_pct":    pnl / (pos["shares"] * pos["entry_price"]) * 100,
                            "exit_reason":   "nifty_100ma",
                        })
                    peak_equity = cash  # reset HWM after regime exit
                market_regime_ok = new_regime
            else:
                market_regime_ok = True  # default if no signal yet

        # ── Mark-to-market ──────────────────────────────────────────────
        open_value = sum(
            pos["shares"] * all_data[sym].loc[date]["close"]
            for sym, pos in open_positions.items()
            if sym in all_data and date in all_data[sym].index
        )
        current_equity = cash + open_value

        # ── Update high-watermark ────────────────────────────────────────
        if current_equity > peak_equity:
            peak_equity = current_equity

        # ── HWM -15% portfolio stop — exit at TODAY'S CLOSE ─────────────
        dd_from_peak = (current_equity - peak_equity) / peak_equity
        if dd_from_peak <= -HWM_DD_STOP and open_positions:
            stop_events.append({
                "date":             date,
                "peak_equity":      peak_equity,
                "equity_at_stop":   current_equity,
                "dd_pct":           dd_from_peak * 100,
                "positions_closed": len(open_positions),
            })
            for sym in list(open_positions.keys()):
                pos        = open_positions.pop(sym)
                exit_price = get_hwm_exit_price(all_data[sym], date)
                proceeds   = pos["shares"] * exit_price
                cash      += proceeds
                pnl        = proceeds - pos["shares"] * pos["entry_price"]
                trades.append({
                    "symbol":        sym,
                    "entry_date":    pos["entry_date"],
                    "exit_date":     date,
                    "entry_price":   pos["entry_price"],
                    "exit_price":    exit_price,
                    "shares":        pos["shares"],
                    "position_size": pos["position_size"],
                    "pnl":           pnl,
                    "return_pct":    pnl / (pos["shares"] * pos["entry_price"]) * 100,
                    "exit_reason":   "hwm_stop",
                })
            peak_equity    = cash
            current_equity = cash

        # ── Normal trail exits — next trading day open ───────────────────
        to_exit = [
            sym for sym, pos in open_positions.items()
            if all_data.get(sym) is not None
            and date in all_data[sym].index
            and all_data[sym].loc[date]["exit_signal"]
        ]
        for sym in to_exit:
            pos        = open_positions.pop(sym)
            exit_price = get_trail_exit_price(all_data[sym], date)
            proceeds   = pos["shares"] * exit_price
            cash      += proceeds
            pnl        = proceeds - pos["shares"] * pos["entry_price"]
            trades.append({
                "symbol":        sym,
                "entry_date":    pos["entry_date"],
                "exit_date":     date,
                "entry_price":   pos["entry_price"],
                "exit_price":    exit_price,
                "shares":        pos["shares"],
                "position_size": pos["position_size"],
                "pnl":           pnl,
                "return_pct":    pnl / (pos["shares"] * pos["entry_price"]) * 100,
                "exit_reason":   "trail_stop",
            })

        # ── Entries (only when both filters allow) ───────────────────────
        daily_ok   = bool(above_200.get(date, False)) if use_200ma else True
        entries_ok = daily_ok and market_regime_ok
        slots_free = MAX_POSITIONS - len(open_positions)

        if entries_ok and slots_free > 0:
            open_value_now = sum(
                pos["shares"] * all_data[sym].loc[date]["close"]
                for sym, pos in open_positions.items()
                if sym in all_data and date in all_data[sym].index
            )
            position_size = min((cash + open_value_now) * POSITION_PCT, MAX_POSITION_CAP)

            candidates = [
                (sym, df)
                for sym, df in all_data.items()
                if sym not in open_positions
                and date in df.index
                and df.loc[date]["entry_signal"]
            ]
            for sym, df in candidates[:slots_free]:
                idx = df.index.searchsorted(date)
                next_idx = idx + 1
                if next_idx >= len(df):
                    continue
                entry_price = df.iloc[next_idx]["open"]
                if entry_price <= 0:
                    continue
                shares = int(position_size / entry_price)
                if shares == 0 or shares * entry_price > cash:
                    continue
                cash -= shares * entry_price
                open_positions[sym] = {
                    "shares":        shares,
                    "entry_price":   entry_price,
                    "entry_date":    df.index[next_idx],
                    "position_size": shares * entry_price,
                }

        # ── End-of-day snapshot ──────────────────────────────────────────
        open_value = sum(
            pos["shares"] * all_data[sym].loc[date]["close"]
            for sym, pos in open_positions.items()
            if sym in all_data and date in all_data[sym].index
        )
        daily_equity.append({
            "date":           date,
            "equity":         cash + open_value,
            "open_positions": len(open_positions),
            "regime_ok":      int(market_regime_ok),
        })

    # Close remaining at last close
    for sym, pos in open_positions.items():
        df         = all_data[sym]
        last_price = df["close"].iloc[-1]
        proceeds   = pos["shares"] * last_price
        cash      += proceeds
        pnl        = proceeds - pos["shares"] * pos["entry_price"]
        trades.append({
            "symbol": sym, "entry_date": pos["entry_date"],
            "exit_date": df.index[-1], "entry_price": pos["entry_price"],
            "exit_price": last_price, "shares": pos["shares"],
            "position_size": pos["position_size"],
            "pnl": pnl,
            "return_pct": pnl / (pos["shares"] * pos["entry_price"]) * 100,
            "exit_reason": "eod",
        })

    eq_df = pd.DataFrame(daily_equity).set_index("date")
    tr_df = pd.DataFrame(trades) if trades else pd.DataFrame()
    return {
        "equity": eq_df, "trades": tr_df,
        "stop_events": stop_events, "monthly_exits": monthly_exits,
    }


def print_summary(label: str, result: dict):
    eq = result["equity"]["equity"]
    tr = result["trades"]

    final  = eq.iloc[-1]
    pk     = eq.cummax()
    dd     = (eq - pk) / pk * 100
    max_dd = dd.min()
    years  = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr   = ((final / STARTING_CAPITAL) ** (1 / years) - 1) * 100
    wins   = tr[tr["pnl"] > 0]; losses = tr[tr["pnl"] <= 0]
    wr     = len(wins) / len(tr) * 100 if len(tr) else 0
    pf     = wins["pnl"].sum() / abs(losses["pnl"].sum()) if len(losses) else 0

    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(f"  Final Equity      : ₹{final:>15,.0f}")
    print(f"  Net P&L           : ₹{final - STARTING_CAPITAL:>+15,.0f}")
    print(f"  CAGR              : {cagr:>8.2f}%")
    print(f"  Max Drawdown      : {max_dd:>8.2f}%")
    print(f"  Total Trades      : {len(tr):>6}")
    print(f"  Win Rate          : {wr:>8.1f}%")
    print(f"  Profit Factor     : {pf:>8.2f}")
    print(f"  Avg Positions     : {result['equity']['open_positions'].mean():>8.1f}")
    print(f"  HWM Stops Fired   : {len(result['stop_events']):>6}")
    print(f"  Monthly 100MA Exits: {len(result['monthly_exits']):>5}")
    if "position_size" in tr.columns:
        print(f"  Avg Position ₹    : ₹{tr['position_size'].mean():>14,.0f}")
        print(f"  Max Position ₹    : ₹{tr['position_size'].max():>14,.0f}")

    if result["monthly_exits"]:
        print(f"\n  Monthly 100MA Exit Events:")
        print(f"  {'Date':<12} {'Equity Before':>14} {'Pos':>5}")
        print("  " + "-"*36)
        for ev in result["monthly_exits"]:
            print(f"  {str(ev['date'])[:10]:<12} "
                  f"₹{ev['equity_before']:>12,.0f} "
                  f"{ev['positions_closed']:>5}")

    if result["stop_events"]:
        print(f"\n  HWM Stop Events:")
        print(f"  {'Date':<12} {'Peak':>14} {'Equity':>14} {'DD%':>8} {'Pos':>5}")
        print("  " + "-"*58)
        for ev in result["stop_events"]:
            print(f"  {str(ev['date'])[:10]:<12} "
                  f"₹{ev['peak_equity']:>12,.0f} "
                  f"₹{ev['equity_at_stop']:>12,.0f} "
                  f"{ev['dd_pct']:>7.1f}% "
                  f"{ev['positions_closed']:>5}")

    print(f"\n  {'Year':<6} {'Start':>14} {'End':>14} {'P&L':>14} {'Ret':>7}")
    print("  " + "-"*62)
    for yr, grp in result["equity"]["equity"].resample("YE"):
        s = grp.iloc[0]; e = grp.iloc[-1]
        p = e - s; r = p / s * 100
        print(f"  {yr.year:<6} ₹{s:>12,.0f} ₹{e:>12,.0f} ₹{p:>+12,.0f} {r:>+6.1f}%")
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
    above_200, above_100_monthly = load_nifty(Path(__file__).parent / "nifty_daily.parquet")

    print(f"Position sizing   : {POSITION_PCT*100:.0f}% of current equity (cap ₹{MAX_POSITION_CAP/1e5:.0f}L)")
    print(f"Portfolio stop    : HWM -15% (exit at close)")
    print(f"Daily filter      : Nifty 200MA (block entries)")
    print(f"Monthly filter    : Nifty 100MA (force-exit + block entries at month-end)\n")

    print("Running V13a — V12b baseline (no monthly filter, for comparison)...")
    r13a = run_v13(all_data, all_dates, above_200, above_100_monthly,
                   use_200ma=True, use_100ma_monthly=False)

    print("Running V13b — + Nifty 100MA monthly exit/entry filter...")
    r13b = run_v13(all_data, all_dates, above_200, above_100_monthly,
                   use_200ma=True, use_100ma_monthly=True)

    print_summary("V13a — Dynamic 1% + 200MA daily + HWM -15%  (baseline, no monthly)", r13a)
    print_summary("V13b — Dynamic 1% + 200MA daily + HWM -15%  + 100MA monthly exit",  r13b)

    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    for tag, r in [("v13a", r13a), ("v13b", r13b)]:
        r["trades"].to_csv(out / f"trades_55day_{tag}.csv", index=False)
        r["equity"].to_csv(out / f"equity_55day_{tag}.csv")

    # ── Master comparison ──────────────────────────────────────────────
    print("\n" + "="*75)
    print("  MASTER COMPARISON")
    print("="*75)
    print(f"  {'Version':<52} {'CAGR':>7} {'Max DD':>9}")
    print("  " + "-"*72)

    prev = {
        "V1  Fixed ₹10K, no filter":                  "equity_55day_v1.csv",
        "V2  Fixed ₹10K + Nifty 200MA":               "equity_55day_v2.csv",
        "V10b Fixed ₹10K + Nifty + HWM stop":         "equity_55day_v10b.csv",
        "V12b Dynamic 1% + 200MA + HWM (next open)":  "equity_55day_v12b.csv",
    }
    for name, csv in prev.items():
        try:
            eq    = pd.read_csv(out / csv, index_col=0, parse_dates=True)["equity"]
            years = (eq.index[-1] - eq.index[0]).days / 365.25
            cagr  = ((eq.iloc[-1] / STARTING_CAPITAL) ** (1/years) - 1) * 100
            pk    = eq.cummax(); mdd = ((eq - pk) / pk * 100).min()
            print(f"  {name:<52} {cagr:>6.2f}% {mdd:>8.2f}%")
        except FileNotFoundError:
            print(f"  {name:<52}  (not found)")

    for name, r in [
        ("V13a Dynamic 1% + 200MA + HWM (baseline)",            r13a),
        ("V13b Dynamic 1% + 200MA + HWM + 100MA monthly exit",  r13b),
    ]:
        eq    = r["equity"]["equity"]
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        cagr  = ((eq.iloc[-1] / STARTING_CAPITAL) ** (1/years) - 1) * 100
        pk    = eq.cummax(); mdd = ((eq - pk) / pk * 100).min()
        print(f"  {name:<52} {cagr:>6.2f}% {mdd:>8.2f}%")
    print()

    # ── Plot ───────────────────────────────────────────────────────────
    fig, axes = plt.subplots(2, 2, figsize=(18, 10))
    fig.suptitle("55-Day Breakout V13 — Nifty 100MA Monthly Exit Filter Added",
                 fontsize=13, fontweight="bold")

    for r, label, ec, dc, ax_eq, ax_dd in [
        (r13a, "V13a — No monthly filter (baseline)", "#2196F3", "#F44336", axes[0][0], axes[1][0]),
        (r13b, "V13b — + Nifty 100MA monthly exit",  "#4CAF50", "#FF9800", axes[0][1], axes[1][1]),
    ]:
        eq = r["equity"]["equity"]
        pk = eq.cummax()
        dd = (eq - pk) / pk * 100

        ax_eq.plot(eq.index, eq / 1e5, color=ec, linewidth=1.3)
        ax_eq.set_title(label, fontweight="bold", fontsize=10)
        ax_eq.set_ylabel("Equity (₹ Lakhs)")
        ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x:.0f}L"))
        ax_eq.axhline(10, color="gray", linestyle="--", linewidth=0.8, label="Start ₹10L")
        ax_eq.fill_between(eq.index, eq / 1e5, 10, where=(eq / 1e5 >= 10), alpha=0.1, color=ec)
        for ev in r["stop_events"]:
            ax_eq.axvline(pd.Timestamp(ev["date"]), color="red", linewidth=0.9, alpha=0.6, label="_hwm")
        for ev in r["monthly_exits"]:
            ax_eq.axvline(pd.Timestamp(ev["date"]), color="purple", linewidth=0.9, alpha=0.6, label="_monthly")
        ax_eq.grid(True, alpha=0.3); ax_eq.legend(fontsize=8)

        ax_dd.fill_between(dd.index, dd, 0, color=dc, alpha=0.5)
        ax_dd.plot(dd.index, dd, color=dc, linewidth=0.8)
        ax_dd.set_title(f"{label} — Drawdown", fontweight="bold", fontsize=10)
        ax_dd.set_ylabel("Drawdown (%)")
        ax_dd.axhline(dd.min(), color="darkred", linestyle="--", linewidth=0.8,
                      label=f"Max DD {dd.min():.1f}%")
        ax_dd.axhline(-15, color="orange", linestyle=":", linewidth=1.2, label="HWM -15%")
        ax_dd.grid(True, alpha=0.3); ax_dd.legend(fontsize=8)

    plt.tight_layout()
    chart_path = out / "equity_drawdown_v13.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    print(f"  Chart saved → {chart_path}")
    plt.show()
