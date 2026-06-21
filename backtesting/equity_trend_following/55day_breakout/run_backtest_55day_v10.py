import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path
from strategy_55day_breakout import (
    load_symbol, compute_signals, DATA_DIR,
    STARTING_CAPITAL, MAX_POSITIONS, POSITION_SIZE
)

HWM_DD_STOP    = 0.15   # exit ALL when equity drops 15% from high-watermark
NIFTY_MA_PERIOD = 200
COOLDOWN_DAYS  = 0      # days to wait before re-entering after stop (0 = immediate)


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


def run_v10(all_data: dict, all_dates: list,
            use_nifty_filter: bool = False,
            nifty_above: pd.Series = None) -> dict:

    open_positions: dict[str, dict] = {}
    trades: list[dict] = []
    daily_equity: list[dict] = []
    cash = STARTING_CAPITAL

    peak_equity   = STARTING_CAPITAL
    stop_events   = []
    cooldown_until = None   # date after which entries re-open

    for date in all_dates:

        # ── Mark-to-market equity ──────────────────────────────────────
        open_value = sum(
            pos["shares"] * all_data[sym].loc[date]["close"]
            for sym, pos in open_positions.items()
            if sym in all_data and date in all_data[sym].index
        )
        current_equity = cash + open_value

        # ── Update high-watermark ──────────────────────────────────────
        if current_equity > peak_equity:
            peak_equity = current_equity

        # ── High-watermark drawdown stop ──────────────────────────────
        dd_from_peak = (current_equity - peak_equity) / peak_equity
        if dd_from_peak <= -HWM_DD_STOP and open_positions:
            stop_events.append({
                "date":             date,
                "peak_equity":      peak_equity,
                "equity_at_stop":   current_equity,
                "dd_pct":           dd_from_peak * 100,
                "positions_closed": len(open_positions),
            })

            # Force-exit all positions at next open (or same-day open if date missing)
            for sym in list(open_positions.keys()):
                pos = open_positions.pop(sym)
                exit_price = get_exit_price(all_data[sym], date)
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
                    "exit_reason": "hwm_stop",
                })

            # Reset high-watermark to post-exit cash (new cycle)
            peak_equity   = cash
            current_equity = cash

            if COOLDOWN_DAYS > 0:
                date_idx = all_dates.index(date)
                resume_idx = min(date_idx + COOLDOWN_DAYS, len(all_dates) - 1)
                cooldown_until = all_dates[resume_idx]

        # ── Normal trail exits (20-day low) ───────────────────────────
        to_exit = []
        for sym, pos in open_positions.items():
            df = all_data.get(sym)
            if df is None or date not in df.index:
                continue
            if df.loc[date]["exit_signal"]:
                to_exit.append(sym)

        for sym in to_exit:
            pos = open_positions.pop(sym)
            exit_price = get_exit_price(all_data[sym], date)
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
                "exit_reason": "trail_stop",
            })

        # ── Entries ────────────────────────────────────────────────────
        in_cooldown = cooldown_until is not None and date <= cooldown_until
        market_ok   = True
        if use_nifty_filter and nifty_above is not None:
            market_ok = bool(nifty_above.get(date, False))

        if market_ok and not in_cooldown:
            slots_free = MAX_POSITIONS - len(open_positions)
            if slots_free > 0:
                candidates = [
                    (sym, df, date)
                    for sym, df in all_data.items()
                    if sym not in open_positions
                    and date in df.index
                    and df.loc[date]["entry_signal"]
                ]
                for sym, df, sig_date in candidates[:slots_free]:
                    idx = df.index.searchsorted(sig_date)
                    next_idx = idx + 1
                    if next_idx >= len(df):
                        continue
                    entry_price = df.iloc[next_idx]["open"]
                    if entry_price <= 0:
                        continue
                    shares = int(POSITION_SIZE / entry_price)
                    if shares == 0 or shares * entry_price > cash:
                        continue
                    cash -= shares * entry_price
                    open_positions[sym] = {
                        "shares":      shares,
                        "entry_price": entry_price,
                        "entry_date":  df.index[next_idx],
                    }

        # ── End-of-day equity snapshot ─────────────────────────────────
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

    print(f"\n{'='*65}")
    print(f"  {label}")
    print(f"{'='*65}")
    print(f"  Final Equity    : ₹{final:>15,.0f}")
    print(f"  Net P&L         : ₹{final - STARTING_CAPITAL:>+15,.0f}")
    print(f"  CAGR            : {cagr:>8.2f}%")
    print(f"  Max Drawdown    : {max_dd:>8.2f}%")
    print(f"  Total Trades    : {len(tr):>6}")
    print(f"  Win Rate        : {wr:>8.1f}%")
    print(f"  Profit Factor   : {pf:>8.2f}")
    print(f"  Avg Positions   : {result['equity']['open_positions'].mean():>8.1f}")
    print(f"  HWM Stops Fired : {len(result['stop_events']):>6}")

    if result["stop_events"]:
        print(f"\n  Stop Events (fired when equity fell {HWM_DD_STOP*100:.0f}% below peak):")
        print(f"  {'Date':<12} {'Peak':>14} {'Equity':>14} {'DD%':>8} {'Pos':>5}")
        print("  " + "-"*60)
        for ev in result["stop_events"]:
            print(f"  {str(ev['date'])[:10]:<12} "
                  f"₹{ev['peak_equity']:>12,.0f} "
                  f"₹{ev['equity_at_stop']:>12,.0f} "
                  f"{ev['dd_pct']:>7.1f}% "
                  f"{ev['positions_closed']:>5}")

    print(f"\n  {'Year':<6} {'Start':>14} {'End':>14} {'P&L':>14} {'Ret':>7}")
    print("  " + "-"*60)
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

    print(f"Stop rule: exit ALL when equity falls {HWM_DD_STOP*100:.0f}% below high-watermark\n")

    print("Running V10a — HWM -15% stop (no Nifty filter)...")
    r10a = run_v10(all_data, all_dates, use_nifty_filter=False)

    print("Running V10b — HWM -15% stop + Nifty 200MA filter...")
    r10b = run_v10(all_data, all_dates, use_nifty_filter=True, nifty_above=nifty_above)

    print_summary("V10a — HWM -15% stop  (no Nifty filter)", r10a)
    print_summary("V10b — HWM -15% stop  + Nifty 200MA filter", r10b)

    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    for tag, r in [("v10a", r10a), ("v10b", r10b)]:
        r["trades"].to_csv(out / f"trades_55day_{tag}.csv", index=False)
        r["equity"].to_csv(out / f"equity_55day_{tag}.csv")

    # ── Master comparison ──────────────────────────────────────────────
    print("\n" + "="*72)
    print("  MASTER COMPARISON")
    print("="*72)
    print(f"  {'Version':<44} {'CAGR':>7} {'Max DD':>9} {'PF':>6}")
    print("  " + "-"*70)

    prev = {
        "V1  No filter":                 "equity_55day_v1.csv",
        "V2  Nifty 200MA block entries": "equity_55day_v2.csv",
        "V6a Per-trade -15% stop":       "equity_55day_v6a.csv",
        "V6b Per-trade stop + Nifty":    "equity_55day_v6b.csv",
        "V8a Cycle-loss 15% stop":       "equity_55day_v8a.csv",
        "V8b Cycle-loss + Nifty":        "equity_55day_v8b.csv",
    }
    for name, csv in prev.items():
        try:
            eq = pd.read_csv(out / csv, index_col=0, parse_dates=True)["equity"]
            tr = pd.read_csv(out / csv.replace("equity_", "trades_"))
            years = (eq.index[-1] - eq.index[0]).days / 365.25
            cagr  = ((eq.iloc[-1] / STARTING_CAPITAL) ** (1/years) - 1) * 100
            pk    = eq.cummax(); mdd = ((eq - pk) / pk * 100).min()
            w = tr[tr["pnl"] > 0]; l = tr[tr["pnl"] <= 0]
            pf = w["pnl"].sum() / abs(l["pnl"].sum()) if len(l) else 0
            print(f"  {name:<44} {cagr:>6.2f}% {mdd:>8.2f}%  {pf:>5.2f}")
        except FileNotFoundError:
            print(f"  {name:<44}  (CSV not found)")

    for name, r in [("V10a HWM -15% stop (no filter)", r10a),
                    ("V10b HWM -15% stop + Nifty 200MA", r10b)]:
        eq = r["equity"]["equity"]; tr = r["trades"]
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        cagr  = ((eq.iloc[-1] / STARTING_CAPITAL) ** (1/years) - 1) * 100
        pk    = eq.cummax(); mdd = ((eq - pk) / pk * 100).min()
        w = tr[tr["pnl"] > 0]; l = tr[tr["pnl"] <= 0]
        pf = w["pnl"].sum() / abs(l["pnl"].sum()) if len(l) else 0
        print(f"  {name:<44} {cagr:>6.2f}% {mdd:>8.2f}%  {pf:>5.2f}")
    print()

    # ── Plot ───────────────────────────────────────────────────────────
    fig, axes = plt.subplots(2, 2, figsize=(18, 10))
    fig.suptitle("55-Day Breakout V10 — High-Watermark -15% Portfolio Stop",
                 fontsize=14, fontweight="bold")

    for r, label, ec, dc, ax_eq, ax_dd in [
        (r10a, "V10a — HWM -15% (no filter)",      "#2196F3", "#F44336", axes[0][0], axes[1][0]),
        (r10b, "V10b — HWM -15% + Nifty 200MA",    "#4CAF50", "#FF9800", axes[0][1], axes[1][1]),
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
            ax_eq.axvline(pd.Timestamp(ev["date"]), color="red", linewidth=0.8, alpha=0.6)
        ax_eq.grid(True, alpha=0.3); ax_eq.legend(fontsize=8)

        ax_dd.fill_between(dd.index, dd, 0, color=dc, alpha=0.5)
        ax_dd.plot(dd.index, dd, color=dc, linewidth=0.8)
        ax_dd.set_title(f"{label.split('—')[0].strip()} — Drawdown",
                        fontweight="bold", fontsize=10)
        ax_dd.set_ylabel("Drawdown (%)")
        ax_dd.axhline(dd.min(), color="darkred", linestyle="--", linewidth=0.8,
                      label=f"Max DD {dd.min():.1f}%")
        ax_dd.axhline(-HWM_DD_STOP * 100, color="orange", linestyle=":",
                      linewidth=1.0, label=f"Stop line -{HWM_DD_STOP*100:.0f}%")
        ax_dd.grid(True, alpha=0.3); ax_dd.legend(fontsize=8)

    plt.tight_layout()
    chart_path = out / "equity_drawdown_v10.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    print(f"  Chart saved → {chart_path}")
    plt.show()
