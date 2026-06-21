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

POSITION_PCT    = 0.01    # 1% of current equity per trade (compounding)
MAX_POSITION_CAP = 200_000  # never risk more than ₹2L per single position
HWM_DD_STOP     = 0.15    # exit ALL when equity falls 15% below high-watermark
NIFTY_MA_PERIOD = 200


def load_nifty(path: Path) -> pd.Series:
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    ma = df["close"].rolling(NIFTY_MA_PERIOD).mean()
    return df["close"] > ma


def get_exit_price(df: pd.DataFrame, date) -> float:
    idx = df.index.searchsorted(date)
    if idx < len(df) and df.index[idx] == date:
        next_idx = idx + 1
        return df.iloc[next_idx]["open"] if next_idx < len(df) else df.iloc[idx]["close"]
    elif idx < len(df):
        return df.iloc[idx]["open"]
    return df.iloc[-1]["close"]


def run_v11(all_data: dict, all_dates: list,
            nifty_above: pd.Series,
            max_pos_cap: float = MAX_POSITION_CAP) -> dict:

    open_positions: dict[str, dict] = {}
    trades:        list[dict] = []
    daily_equity:  list[dict] = []
    cash = STARTING_CAPITAL

    peak_equity = STARTING_CAPITAL
    stop_events = []

    for date in all_dates:

        # ── Mark-to-market ─────────────────────────────────────────────
        open_value = sum(
            pos["shares"] * all_data[sym].loc[date]["close"]
            for sym, pos in open_positions.items()
            if sym in all_data and date in all_data[sym].index
        )
        current_equity = cash + open_value

        # ── Update high-watermark ───────────────────────────────────────
        if current_equity > peak_equity:
            peak_equity = current_equity

        # ── HWM -15% portfolio stop ────────────────────────────────────
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
                pos = open_positions.pop(sym)
                exit_price = get_exit_price(all_data[sym], date)
                proceeds   = pos["shares"] * exit_price
                cash      += proceeds
                pnl        = proceeds - pos["shares"] * pos["entry_price"]
                trades.append({
                    "symbol":      sym,
                    "entry_date":  pos["entry_date"],
                    "exit_date":   date,
                    "entry_price": pos["entry_price"],
                    "exit_price":  exit_price,
                    "shares":      pos["shares"],
                    "position_size": pos["position_size"],
                    "pnl":         pnl,
                    "return_pct":  pnl / (pos["shares"] * pos["entry_price"]) * 100,
                    "exit_reason": "hwm_stop",
                })
            peak_equity    = cash
            current_equity = cash

        # ── Normal trail exits (20-day low) ────────────────────────────
        to_exit = [
            sym for sym, pos in open_positions.items()
            if all_data.get(sym) is not None
            and date in all_data[sym].index
            and all_data[sym].loc[date]["exit_signal"]
        ]
        for sym in to_exit:
            pos        = open_positions.pop(sym)
            exit_price = get_exit_price(all_data[sym], date)
            proceeds   = pos["shares"] * exit_price
            cash      += proceeds
            pnl        = proceeds - pos["shares"] * pos["entry_price"]
            trades.append({
                "symbol":      sym,
                "entry_date":  pos["entry_date"],
                "exit_date":   date,
                "entry_price": pos["entry_price"],
                "exit_price":  exit_price,
                "shares":      pos["shares"],
                "position_size": pos["position_size"],
                "pnl":         pnl,
                "return_pct":  pnl / (pos["shares"] * pos["entry_price"]) * 100,
                "exit_reason": "trail_stop",
            })

        # ── Entries ─────────────────────────────────────────────────────
        market_ok  = bool(nifty_above.get(date, False))
        slots_free = MAX_POSITIONS - len(open_positions)

        if market_ok and slots_free > 0:
            # Current equity for dynamic sizing
            open_value_now = sum(
                pos["shares"] * all_data[sym].loc[date]["close"]
                for sym, pos in open_positions.items()
                if sym in all_data and date in all_data[sym].index
            )
            current_equity_now = cash + open_value_now
            position_size = min(current_equity_now * POSITION_PCT, max_pos_cap)

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

        # ── End-of-day snapshot ─────────────────────────────────────────
        open_value = sum(
            pos["shares"] * all_data[sym].loc[date]["close"]
            for sym, pos in open_positions.items()
            if sym in all_data and date in all_data[sym].index
        )
        daily_equity.append({
            "date":            date,
            "equity":          cash + open_value,
            "open_positions":  len(open_positions),
        })

    # Close remaining
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
    return {"equity": eq_df, "trades": tr_df, "stop_events": stop_events}


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

    print(f"\n{'='*68}")
    print(f"  {label}")
    print(f"{'='*68}")
    print(f"  Final Equity    : ₹{final:>15,.0f}")
    print(f"  Net P&L         : ₹{final - STARTING_CAPITAL:>+15,.0f}")
    print(f"  CAGR            : {cagr:>8.2f}%")
    print(f"  Max Drawdown    : {max_dd:>8.2f}%")
    print(f"  Total Trades    : {len(tr):>6}")
    print(f"  Win Rate        : {wr:>8.1f}%")
    print(f"  Profit Factor   : {pf:>8.2f}")
    print(f"  Avg Positions   : {result['equity']['open_positions'].mean():>8.1f}")
    print(f"  HWM Stops Fired : {len(result['stop_events']):>6}")

    if "position_size" in tr.columns:
        print(f"  Avg Position ₹  : ₹{tr['position_size'].mean():>14,.0f}")
        print(f"  Max Position ₹  : ₹{tr['position_size'].max():>14,.0f}")

    if result["stop_events"]:
        print(f"\n  Stop Events:")
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

    all_dates   = sorted(set().union(*[set(df.index) for df in all_data.values()]))
    nifty_above = load_nifty(Path(__file__).parent / "nifty_daily.parquet")

    print(f"Position sizing : {POSITION_PCT*100:.0f}% of current equity (capped at ₹{MAX_POSITION_CAP/1e5:.0f}L)")
    print(f"Portfolio stop  : exit ALL if equity drops {HWM_DD_STOP*100:.0f}% below high-watermark")
    print(f"Market filter   : Nifty 200MA\n")

    print("Running V11a — Dynamic 1% + Nifty filter + HWM -15% stop (no cap)...")
    r11a = run_v11(all_data, all_dates, nifty_above, max_pos_cap=float("inf"))

    print(f"Running V11b — Dynamic 1% + Nifty filter + HWM -15% stop (cap ₹{MAX_POSITION_CAP/1e5:.0f}L)...")
    r11b = run_v11(all_data, all_dates, nifty_above, max_pos_cap=MAX_POSITION_CAP)

    print_summary("V11a — Dynamic 1% + Nifty + HWM stop  (no position cap)", r11a)
    print_summary(f"V11b — Dynamic 1% + Nifty + HWM stop  (cap ₹{MAX_POSITION_CAP/1e5:.0f}L/position)", r11b)

    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    for tag, r in [("v11a", r11a), ("v11b", r11b)]:
        r["trades"].to_csv(out / f"trades_55day_{tag}.csv", index=False)
        r["equity"].to_csv(out / f"equity_55day_{tag}.csv")

    # ── Master comparison ──────────────────────────────────────────────
    print("\n" + "="*75)
    print("  MASTER COMPARISON")
    print("="*75)
    print(f"  {'Version':<50} {'CAGR':>7} {'Max DD':>9}")
    print("  " + "-"*70)

    prev = {
        "V1  Fixed ₹10K, no filter":              "equity_55day_v1.csv",
        "V2  Fixed ₹10K + Nifty 200MA":           "equity_55day_v2.csv",
        "V10b Fixed ₹10K + Nifty + HWM stop":     "equity_55day_v10b.csv",
    }
    for name, csv in prev.items():
        try:
            eq    = pd.read_csv(out / csv, index_col=0, parse_dates=True)["equity"]
            years = (eq.index[-1] - eq.index[0]).days / 365.25
            cagr  = ((eq.iloc[-1] / STARTING_CAPITAL) ** (1/years) - 1) * 100
            pk    = eq.cummax(); mdd = ((eq - pk) / pk * 100).min()
            print(f"  {name:<50} {cagr:>6.2f}% {mdd:>8.2f}%")
        except FileNotFoundError:
            print(f"  {name:<50}  (not found)")

    for name, r in [
        ("V11a Dynamic 1% + Nifty + HWM (no cap)",       r11a),
        (f"V11b Dynamic 1% + Nifty + HWM (cap ₹{MAX_POSITION_CAP/1e5:.0f}L)", r11b),
    ]:
        eq    = r["equity"]["equity"]
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        cagr  = ((eq.iloc[-1] / STARTING_CAPITAL) ** (1/years) - 1) * 100
        pk    = eq.cummax(); mdd = ((eq - pk) / pk * 100).min()
        print(f"  {name:<50} {cagr:>6.2f}% {mdd:>8.2f}%")
    print()

    # ── Plot ───────────────────────────────────────────────────────────
    fig, axes = plt.subplots(2, 2, figsize=(18, 10))
    fig.suptitle("55-Day Breakout V11 — Dynamic 1% Compounding + Nifty Filter + HWM -15% Stop",
                 fontsize=13, fontweight="bold")

    for r, label, ec, dc, ax_eq, ax_dd in [
        (r11a, "V11a — No position cap",    "#2196F3", "#F44336", axes[0][0], axes[1][0]),
        (r11b, f"V11b — Cap ₹{MAX_POSITION_CAP/1e5:.0f}L/position", "#4CAF50", "#FF9800", axes[0][1], axes[1][1]),
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
            ax_eq.axvline(pd.Timestamp(ev["date"]), color="red", linewidth=0.9, alpha=0.6)
        ax_eq.grid(True, alpha=0.3); ax_eq.legend(fontsize=8)

        ax_dd.fill_between(dd.index, dd, 0, color=dc, alpha=0.5)
        ax_dd.plot(dd.index, dd, color=dc, linewidth=0.8)
        ax_dd.set_title(f"{label} — Drawdown", fontweight="bold", fontsize=10)
        ax_dd.set_ylabel("Drawdown (%)")
        ax_dd.axhline(dd.min(), color="darkred", linestyle="--", linewidth=0.8,
                      label=f"Max DD {dd.min():.1f}%")
        ax_dd.axhline(-15, color="orange", linestyle=":", linewidth=1.0, label="Stop -15%")
        ax_dd.grid(True, alpha=0.3); ax_dd.legend(fontsize=8)

    plt.tight_layout()
    chart_path = out / "equity_drawdown_v11.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    print(f"  Chart saved → {chart_path}")
    plt.show()
