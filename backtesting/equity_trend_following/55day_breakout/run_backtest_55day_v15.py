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

POSITION_PCT  = 0.01        # 1% of current equity (compounding)
HWM_DD_STOP   = 0.15        # exit ALL when equity falls 15% from peak
NIFTY_MA_PERIOD = 200


def load_nifty(path: Path):
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    return df["close"]   # raw close series


def get_hwm_exit_price(df: pd.DataFrame, date) -> float:
    if date in df.index:
        return df.loc[date]["close"]
    idx = df.index.searchsorted(date)
    return df.iloc[idx]["close"] if idx < len(df) else df.iloc[-1]["close"]


def get_trail_exit_price(df: pd.DataFrame, date) -> float:
    idx = df.index.searchsorted(date)
    if idx < len(df) and df.index[idx] == date:
        next_idx = idx + 1
        return df.iloc[next_idx]["open"] if next_idx < len(df) else df.iloc[idx]["close"]
    elif idx < len(df):
        return df.iloc[idx]["open"]
    return df.iloc[-1]["close"]


def run_v15(all_data: dict, all_dates: list,
            nifty_close: pd.Series,
            reentry_mode: str = "immediate") -> dict:
    """
    reentry_mode:
        "immediate"  — re-enter on next signal (baseline V14b)
        "nifty_recovery" — wait until Nifty closes above its level at stop date (option 4)
        "fresh_high"     — per-stock: only re-enter when 55-day high exceeds the high at stop (option 5)
        "both"           — both conditions must be met
    """

    open_positions: dict[str, dict] = {}
    trades:        list[dict] = []
    daily_equity:  list[dict] = []
    cash = STARTING_CAPITAL

    peak_equity          = STARTING_CAPITAL
    stop_events          = []

    # Option 4 state
    nifty_block_level    = None   # Nifty must recover above this before re-entry

    # Option 5 state: {sym: high55_at_stop}  — must exceed this to re-enter
    blocked_high55: dict[str, float] = {}

    for date in all_dates:

        nifty_today = float(nifty_close.get(date, float("nan")))

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

        # ── HWM -15% stop ────────────────────────────────────────────────
        dd_from_peak = (current_equity - peak_equity) / peak_equity
        if dd_from_peak <= -HWM_DD_STOP and open_positions:
            stop_events.append({
                "date":             date,
                "peak_equity":      peak_equity,
                "equity_at_stop":   current_equity,
                "dd_pct":           dd_from_peak * 100,
                "positions_closed": len(open_positions),
                "nifty_at_stop":    nifty_today,
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

                # Option 5: record the 55-day high at stop for each exited stock
                if reentry_mode in ("fresh_high", "both"):
                    df_sym = all_data[sym]
                    if date in df_sym.index:
                        blocked_high55[sym] = float(df_sym.loc[date]["high55"])

            # Option 4: block entries until Nifty recovers above today's level
            if reentry_mode in ("nifty_recovery", "both"):
                nifty_block_level = nifty_today

            peak_equity    = cash
            current_equity = cash

        # ── Normal trail exits ───────────────────────────────────────────
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

        # ── Check re-entry conditions ────────────────────────────────────
        # Option 4: clear block once Nifty recovers
        if nifty_block_level is not None and not pd.isna(nifty_today):
            if nifty_today >= nifty_block_level:
                nifty_block_level = None   # unblock

        nifty_ok = (nifty_block_level is None)

        # ── Entries ──────────────────────────────────────────────────────
        slots_free = MAX_POSITIONS - len(open_positions)
        if nifty_ok and slots_free > 0:
            open_value_now = sum(
                pos["shares"] * all_data[sym].loc[date]["close"]
                for sym, pos in open_positions.items()
                if sym in all_data and date in all_data[sym].index
            )
            position_size = (cash + open_value_now) * POSITION_PCT

            candidates = [
                (sym, df)
                for sym, df in all_data.items()
                if sym not in open_positions
                and date in df.index
                and df.loc[date]["entry_signal"]
            ]

            for sym, df in candidates[:slots_free]:
                # Option 5: check if 55-day high is genuinely new
                if reentry_mode in ("fresh_high", "both") and sym in blocked_high55:
                    current_high55 = float(df.loc[date]["high55"])
                    if current_high55 <= blocked_high55[sym]:
                        continue   # not a fresh high — skip
                    else:
                        del blocked_high55[sym]   # cleared, allow entry

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
            "entry_blocked":  int(not nifty_ok),
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

    blocked_days = result["equity"]["entry_blocked"].sum()

    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(f"  Final Equity    : ₹{final:>15,.0f}")
    print(f"  Net P&L         : ₹{final - STARTING_CAPITAL:>+15,.0f}")
    print(f"  CAGR            : {cagr:>8.2f}%")
    print(f"  Max Drawdown    : {max_dd:>8.2f}%")
    print(f"  Total Trades    : {len(tr):>6}")
    print(f"  Win Rate        : {wr:>8.1f}%")
    print(f"  Profit Factor   : {pf:>8.2f}")
    print(f"  Avg Positions   : {result['equity']['open_positions'].mean():>8.1f}")
    print(f"  HWM Stops Fired : {len(result['stop_events']):>6}")
    print(f"  Days Blocked    : {blocked_days:>6}  (entry blocked after stop)")
    if "position_size" in tr.columns:
        print(f"  Avg Position ₹  : ₹{tr['position_size'].mean():>14,.0f}")
        print(f"  Max Position ₹  : ₹{tr['position_size'].max():>14,.0f}")

    if result["stop_events"]:
        print(f"\n  Stop Events:")
        print(f"  {'Date':<12} {'Peak':>14} {'Equity':>14} {'DD%':>8} {'Nifty':>9}")
        print("  " + "-"*62)
        for ev in result["stop_events"]:
            print(f"  {str(ev['date'])[:10]:<12} "
                  f"₹{ev['peak_equity']:>12,.0f} "
                  f"₹{ev['equity_at_stop']:>12,.0f} "
                  f"{ev['dd_pct']:>7.1f}% "
                  f"{ev.get('nifty_at_stop', 0):>8.0f}")

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
    nifty_close = load_nifty(Path(__file__).parent / "nifty_daily.parquet")

    print(f"Position sizing: 1% of current equity (no cap, no Nifty filter)")
    print(f"HWM stop: -15% from peak (exit at close)\n")

    modes = [
        ("V15a — HWM stop + immediate re-entry (baseline)",    "immediate"),
        ("V15b — HWM stop + Nifty recovery re-entry (opt 4)",  "nifty_recovery"),
        ("V15c — HWM stop + fresh 55-day high re-entry (opt 5)","fresh_high"),
        ("V15d — HWM stop + both conditions (opt 4 + 5)",      "both"),
    ]

    results = {}
    for label, mode in modes:
        print(f"Running {label.split('—')[0].strip()}...")
        results[label] = run_v15(all_data, all_dates, nifty_close, reentry_mode=mode)

    for label, _ in modes:
        print_summary(label, results[label])

    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    for label, mode in modes:
        results[label]["trades"].to_csv(out / f"trades_55day_{mode}.csv", index=False)
        results[label]["equity"].to_csv(out / f"equity_55day_{mode}.csv")

    # ── Comparison table ───────────────────────────────────────────────
    print("\n" + "="*82)
    print("  RE-ENTRY CONDITION COMPARISON (all: dynamic 1%, no Nifty filter, HWM -15%)")
    print("="*82)
    print(f"  {'Version':<50} {'CAGR':>7} {'Max DD':>9} {'Final ₹':>16} {'Stops':>6}")
    print("  " + "-"*92)
    for label, _ in modes:
        r     = results[label]
        eq    = r["equity"]["equity"]
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        cagr  = ((eq.iloc[-1] / STARTING_CAPITAL) ** (1/years) - 1) * 100
        pk    = eq.cummax(); mdd = ((eq - pk) / pk * 100).min()
        name  = label.split("—")[1].strip()
        stops = len(r["stop_events"])
        print(f"  {name:<50} {cagr:>6.2f}% {mdd:>8.2f}%  ₹{eq.iloc[-1]:>13,.0f}  {stops:>5}")
    print()

    # ── Plot ───────────────────────────────────────────────────────────
    fig, axes = plt.subplots(2, 4, figsize=(24, 10))
    fig.suptitle("55-Day Breakout V15 — Re-entry Condition Comparison (HWM -15% stop)",
                 fontsize=13, fontweight="bold")

    colors = ["#2196F3", "#4CAF50", "#FF9800", "#9C27B0"]
    for i, (label, _) in enumerate(modes):
        r  = results[label]
        eq = r["equity"]["equity"]
        pk = eq.cummax()
        dd = (eq - pk) / pk * 100
        name = label.split("—")[1].strip()
        col  = colors[i]

        ax_eq = axes[0][i]
        ax_dd = axes[1][i]

        ax_eq.plot(eq.index, eq / 1e7, color=col, linewidth=1.3)
        ax_eq.set_title(name, fontweight="bold", fontsize=8)
        ax_eq.set_ylabel("Equity (₹ Cr)")
        ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x:.1f}Cr"))
        for ev in r["stop_events"]:
            ax_eq.axvline(pd.Timestamp(ev["date"]), color="red", linewidth=0.9, alpha=0.6)

        # Shade blocked entry periods
        blocked = r["equity"]["entry_blocked"]
        if blocked.sum() > 0:
            ax_eq.fill_between(eq.index, 0, eq.max()/1e7,
                               where=blocked.astype(bool),
                               alpha=0.1, color="red", label="entries blocked")
        ax_eq.grid(True, alpha=0.3); ax_eq.legend(fontsize=7)

        ax_dd.fill_between(dd.index, dd, 0, color=col, alpha=0.4)
        ax_dd.plot(dd.index, dd, color=col, linewidth=0.8)
        ax_dd.set_title(f"Drawdown — {name[:28]}", fontweight="bold", fontsize=8)
        ax_dd.set_ylabel("Drawdown (%)")
        ax_dd.axhline(dd.min(), color="darkred", linestyle="--", linewidth=0.8,
                      label=f"Max DD {dd.min():.0f}%")
        ax_dd.axhline(-15, color="orange", linestyle=":", linewidth=1.0, label="-15% stop")
        ax_dd.grid(True, alpha=0.3); ax_dd.legend(fontsize=7)

    plt.tight_layout()
    chart_path = out / "equity_drawdown_v15.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    print(f"  Chart saved → {chart_path}")
    plt.show()
