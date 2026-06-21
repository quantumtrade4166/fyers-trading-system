import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
from strategy_55day_breakout import (
    load_symbol, compute_signals, DATA_DIR,
    STARTING_CAPITAL, MAX_POSITIONS, POSITION_SIZE
)

YEARLY_LOSS_CAP_PCT = 0.15   # V3/V5: stop new entries if down 15% from year-start equity
NIFTY_MA_PERIOD     = 200    # V4/V5: hard exit + block entries when Nifty < 200MA

# ── Load Nifty filter ──────────────────────────────────────────
def load_nifty(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df["ma200"] = df["close"].rolling(NIFTY_MA_PERIOD).mean()
    df["above_ma"] = df["close"] > df["ma200"]
    # crossed below = today below, yesterday above
    df["crossed_below"] = (~df["above_ma"]) & df["above_ma"].shift(1).fillna(False)
    return df


# ── Core engine (parametric) ───────────────────────────────────
def run_engine(all_data: dict, all_dates: list,
               use_yearly_cap: bool = False,
               use_hard_exit: bool = False,
               nifty: pd.DataFrame = None) -> dict:

    open_positions: dict[str, dict] = {}
    trades: list[dict] = []
    daily_equity: list[dict] = []
    cash = STARTING_CAPITAL

    year_start_equity = STARTING_CAPITAL
    current_year = None
    entries_blocked_this_year = False
    hard_exit_active = False   # True when Nifty < 200MA (V4/V5)

    for date in all_dates:
        # ── Year boundary reset (yearly cap) ──────────────────
        if use_yearly_cap:
            if current_year != date.year:
                current_year = date.year
                # Mark-to-market equity at year start
                open_value = sum(
                    pos["shares"] * all_data[sym].loc[date]["close"]
                    for sym, pos in open_positions.items()
                    if sym in all_data and date in all_data[sym].index
                )
                year_start_equity = cash + open_value
                entries_blocked_this_year = False

        # ── Hard Nifty exit trigger (V4/V5) ───────────────────
        if use_hard_exit and nifty is not None:
            if date in nifty.index:
                above = nifty.loc[date, "above_ma"]
                crossed_below = nifty.loc[date, "crossed_below"]

                if crossed_below and not hard_exit_active:
                    # Nifty just broke below 200MA → exit ALL positions
                    hard_exit_active = True
                    force_exit = list(open_positions.keys())
                    for sym in force_exit:
                        pos = open_positions.pop(sym)
                        df = all_data[sym]
                        sym_dates = df.index.tolist()
                        if date in sym_dates:
                            idx = sym_dates.index(date)
                            exit_price = df.iloc[idx + 1]["open"] if idx + 1 < len(sym_dates) else df.loc[date]["close"]
                        else:
                            exit_price = df["close"].iloc[-1]
                        proceeds = pos["shares"] * exit_price
                        cash += proceeds
                        pnl = proceeds - pos["shares"] * pos["entry_price"]
                        trades.append({
                            "symbol": sym, "entry_date": pos["entry_date"],
                            "exit_date": date, "entry_price": pos["entry_price"],
                            "exit_price": exit_price, "shares": pos["shares"],
                            "pnl": pnl,
                            "return_pct": pnl / (pos["shares"] * pos["entry_price"]) * 100,
                            "exit_reason": "hard_exit",
                        })
                elif above:
                    hard_exit_active = False   # market recovered

        # ── Normal exits (20-day low trail) ───────────────────
        to_exit = []
        for sym, pos in open_positions.items():
            df = all_data.get(sym)
            if df is None or date not in df.index:
                continue
            if df.loc[date]["exit_signal"]:
                to_exit.append(sym)

        for sym in to_exit:
            pos = open_positions.pop(sym)
            df = all_data[sym]
            sym_dates = df.index.tolist()
            idx = sym_dates.index(date)
            exit_price = df.iloc[idx + 1]["open"] if idx + 1 < len(sym_dates) else df.loc[date]["close"]
            proceeds = pos["shares"] * exit_price
            cash += proceeds
            pnl = proceeds - pos["shares"] * pos["entry_price"]
            trades.append({
                "symbol": sym, "entry_date": pos["entry_date"],
                "exit_date": date, "entry_price": pos["entry_price"],
                "exit_price": exit_price, "shares": pos["shares"],
                "pnl": pnl,
                "return_pct": pnl / (pos["shares"] * pos["entry_price"]) * 100,
                "exit_reason": "trail_stop",
            })

        # ── Check yearly loss cap ──────────────────────────────
        if use_yearly_cap and not entries_blocked_this_year:
            open_value = sum(
                pos["shares"] * all_data[sym].loc[date]["close"]
                for sym, pos in open_positions.items()
                if sym in all_data and date in all_data[sym].index
            )
            current_equity = cash + open_value
            loss_pct = (current_equity - year_start_equity) / year_start_equity
            if loss_pct <= -YEARLY_LOSS_CAP_PCT:
                entries_blocked_this_year = True

        # ── Entries ───────────────────────────────────────────
        can_enter = (not entries_blocked_this_year) and (not hard_exit_active)

        # Nifty above MA required for entry (V4/V5 but also applied in V3 without hard exit)
        if use_hard_exit and nifty is not None and date in nifty.index:
            if not nifty.loc[date, "above_ma"]:
                can_enter = False

        if can_enter:
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
                        "shares": shares,
                        "entry_price": entry_price,
                        "entry_date": df.index[idx + 1],
                    }

        # ── Mark-to-market ────────────────────────────────────
        open_value = sum(
            pos["shares"] * all_data[sym].loc[date]["close"]
            for sym, pos in open_positions.items()
            if sym in all_data and date in all_data[sym].index
        )
        equity = cash + open_value
        daily_equity.append({"date": date, "equity": equity,
                              "open_positions": len(open_positions)})

    # Close remaining
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


def summarise(label: str, result: dict):
    eq = result["equity"]["equity"]
    tr = result["trades"]

    final  = eq.iloc[-1]
    peak   = eq.cummax()
    dd     = (eq - peak) / peak * 100
    max_dd = dd.min()
    years  = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr   = ((final / STARTING_CAPITAL) ** (1 / years) - 1) * 100

    wins   = tr[tr["pnl"] > 0] if not tr.empty else pd.DataFrame()
    losses = tr[tr["pnl"] <= 0] if not tr.empty else pd.DataFrame()
    wr     = len(wins) / len(tr) * 100 if not tr.empty else 0
    pf     = wins["pnl"].sum() / abs(losses["pnl"].sum()) if len(losses) else float("inf")

    print(f"\n{'='*62}")
    print(f"  {label}")
    print(f"{'='*62}")
    print(f"  Final Equity   : ₹{final:>12,.0f}   (started ₹{STARTING_CAPITAL:,.0f})")
    print(f"  Net P&L        : ₹{final - STARTING_CAPITAL:>+12,.0f}")
    print(f"  CAGR           : {cagr:>7.2f}%")
    print(f"  Max Drawdown   : {max_dd:>7.2f}%")
    print(f"  Total Trades   : {len(tr):>6}")
    print(f"  Win Rate       : {wr:>7.1f}%")
    print(f"  Profit Factor  : {pf:>7.2f}")
    print(f"  Avg Positions  : {result['equity']['open_positions'].mean():>7.1f}")

    # Year-by-year
    print(f"\n  {'Year':<6} {'Start':>12} {'End':>12} {'P&L':>12} {'Ret':>7}")
    print("  " + "-"*52)
    ann = result["equity"]["equity"].resample("YE")
    for yr, grp in ann:
        s = grp.iloc[0]; e = grp.iloc[-1]
        p = e - s; r = p / s * 100
        print(f"  {yr.year:<6} ₹{s:>10,.0f} ₹{e:>10,.0f} ₹{p:>+10,.0f} {r:>+6.1f}%")
    print()


if __name__ == "__main__":
    # Load all symbol data once
    symbols = [p.stem for p in DATA_DIR.glob("*.parquet")]
    print(f"Loading {len(symbols)} symbols (once, shared across all versions)...")

    all_data = {}
    for sym in symbols:
        df = load_symbol(sym)
        if df is None:
            continue
        df = compute_signals(df)
        if len(df) >= 10:
            all_data[sym] = df

    all_dates = sorted(set().union(*[set(df.index) for df in all_data.values()]))
    print(f"Symbols loaded: {len(all_data)} | Dates: {len(all_dates)}")

    nifty = load_nifty(Path(__file__).parent / "nifty_daily.parquet")

    print(f"\nRunning V3 — Yearly Loss Cap ({YEARLY_LOSS_CAP_PCT*100:.0f}%)...")
    r3 = run_engine(all_data, all_dates, use_yearly_cap=True,  use_hard_exit=False, nifty=nifty)

    print(f"Running V4 — Hard Nifty Exit (below 200MA)...")
    r4 = run_engine(all_data, all_dates, use_yearly_cap=False, use_hard_exit=True,  nifty=nifty)

    print(f"Running V5 — Both Combined...")
    r5 = run_engine(all_data, all_dates, use_yearly_cap=True,  use_hard_exit=True,  nifty=nifty)

    summarise("V3 — Yearly Loss Cap 15%  (entries blocked after −15% in a year)", r3)
    summarise("V4 — Hard Nifty Exit      (exit ALL positions when Nifty < 200MA)", r4)
    summarise("V5 — Combined             (yearly cap 15% + hard Nifty exit)",      r5)

    # Save results
    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    for tag, r in [("v3", r3), ("v4", r4), ("v5", r5)]:
        r["trades"].to_csv(out / f"trades_55day_{tag}.csv", index=False)
        r["equity"].to_csv(out / f"equity_55day_{tag}.csv")

    print(f"\n  All results saved to results/")

    # ── Comparison table ──────────────────────────────────────
    print("\n" + "="*75)
    print("  COMPARISON — ALL 5 VERSIONS")
    print("="*75)
    print(f"  {'Version':<40} {'CAGR':>7} {'Max DD':>9} {'PF':>6} {'Trades':>8}")
    print("  " + "-"*72)

    versions = {
        "V1 — No filter":                    ("equity_55day_v1.csv", None),
        "V2 — Nifty 200MA (block entries)":  ("equity_55day_v2.csv", None),
        "V3 — Yearly cap 15%":               (None, r3),
        "V4 — Hard Nifty exit":              (None, r4),
        "V5 — Combined (cap + hard exit)":   (None, r5),
    }

    def stats(eq_series, tr_df):
        final = eq_series.iloc[-1]
        years = (eq_series.index[-1] - eq_series.index[0]).days / 365.25
        cagr  = ((final / STARTING_CAPITAL) ** (1 / years) - 1) * 100
        pk    = eq_series.cummax()
        mdd   = ((eq_series - pk) / pk * 100).min()
        wins  = tr_df[tr_df["pnl"] > 0] if not tr_df.empty else pd.DataFrame()
        loss  = tr_df[tr_df["pnl"] <= 0] if not tr_df.empty else pd.DataFrame()
        pf    = wins["pnl"].sum() / abs(loss["pnl"].sum()) if len(loss) else 0
        return cagr, mdd, pf, len(tr_df)

    for name, (csv, res) in versions.items():
        if csv:
            eq = pd.read_csv(out / csv, index_col=0, parse_dates=True)["equity"]
            # load corresponding trades
            tcvs = csv.replace("equity_", "trades_")
            tr = pd.read_csv(out / tcvs)
        else:
            eq = res["equity"]["equity"]
            tr = res["trades"]
        c, d, p, n = stats(eq, tr)
        print(f"  {name:<40} {c:>6.2f}% {d:>8.2f}% {p:>6.2f} {n:>8}")
    print()
