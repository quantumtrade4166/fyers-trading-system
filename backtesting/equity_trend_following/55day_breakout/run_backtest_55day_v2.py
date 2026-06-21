import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
from strategy_55day_breakout import (
    load_symbol, compute_signals, print_results,
    DATA_DIR, STARTING_CAPITAL, MAX_POSITIONS, POSITION_SIZE
)

NIFTY_MA_PERIOD = 200

# ── Load Nifty 200-day MA ──────────────────────────────────────
def load_nifty_filter() -> pd.Series:
    path = Path(__file__).parent / "nifty_daily.parquet"
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    ma = df["close"].rolling(NIFTY_MA_PERIOD).mean()
    above_ma = df["close"] > ma   # True = market is in uptrend
    return above_ma


def run_backtest_v2(symbols: list[str], nifty_filter: pd.Series,
                    verbose: bool = False) -> dict:
    all_data: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = load_symbol(sym)
        if df is None:
            continue
        df = compute_signals(df)
        if len(df) < 10:
            continue
        all_data[sym] = df

    all_dates = sorted(set().union(*[set(df.index) for df in all_data.values()]))

    open_positions: dict[str, dict] = {}
    trades: list[dict] = []
    daily_equity: list[dict] = []
    cash = STARTING_CAPITAL

    for date in all_dates:
        # ── Exits (always, regardless of market filter) ───────
        to_exit = []
        for sym, pos in open_positions.items():
            if sym not in all_data:
                continue
            df = all_data[sym]
            if date not in df.index:
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
            pnl = proceeds - (pos["shares"] * pos["entry_price"])
            trades.append({
                "symbol":      sym,
                "entry_date":  pos["entry_date"],
                "exit_date":   date,
                "entry_price": pos["entry_price"],
                "exit_price":  exit_price,
                "shares":      pos["shares"],
                "pnl":         pnl,
                "return_pct":  pnl / (pos["shares"] * pos["entry_price"]) * 100,
            })

        # ── Entries only when Nifty > 200-day MA ──────────────
        market_up = nifty_filter.get(date, False)

        if market_up:
            slots_free = MAX_POSITIONS - len(open_positions)
            if slots_free > 0:
                candidates = []
                for sym, df in all_data.items():
                    if sym in open_positions:
                        continue
                    if date not in df.index:
                        continue
                    if df.loc[date]["entry_signal"]:
                        candidates.append((sym, df, date))

                candidates = candidates[:slots_free]

                for sym, df, sig_date in candidates:
                    sym_dates = df.index.tolist()
                    idx = sym_dates.index(sig_date)
                    if idx + 1 >= len(sym_dates):
                        continue
                    entry_price = df.iloc[idx + 1]["open"]
                    if entry_price <= 0:
                        continue
                    shares = int(POSITION_SIZE / entry_price)
                    if shares == 0:
                        continue
                    cost = shares * entry_price
                    if cost > cash:
                        continue
                    cash -= cost
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
        equity = cash + open_value
        daily_equity.append({"date": date, "equity": equity,
                              "open_positions": len(open_positions)})

    # Close remaining positions at last close
    for sym, pos in open_positions.items():
        df = all_data[sym]
        last_price = df["close"].iloc[-1]
        proceeds = pos["shares"] * last_price
        cash += proceeds
        pnl = proceeds - (pos["shares"] * pos["entry_price"])
        trades.append({
            "symbol":      sym,
            "entry_date":  pos["entry_date"],
            "exit_date":   df.index[-1],
            "entry_price": pos["entry_price"],
            "exit_price":  last_price,
            "shares":      pos["shares"],
            "pnl":         pnl,
            "return_pct":  pnl / (pos["shares"] * pos["entry_price"]) * 100,
        })

    equity_df = pd.DataFrame(daily_equity).set_index("date")
    trades_df  = pd.DataFrame(trades) if trades else pd.DataFrame()
    return {"equity": equity_df, "trades": trades_df, "final_cash": cash}


if __name__ == "__main__":
    symbols = [p.stem for p in DATA_DIR.glob("*.parquet")]
    print(f"Loading {len(symbols)} symbols...")
    print(f"Filter : Nifty 50 close > {NIFTY_MA_PERIOD}-day MA (entries blocked in downtrend)")

    nifty_filter = load_nifty_filter()
    result = run_backtest_v2(symbols, nifty_filter)

    print("\n  [V2 — Nifty 200-day MA Filter]")
    print_results(result)

    if not result["trades"].empty:
        out = Path(__file__).parent / "results"
        out.mkdir(exist_ok=True)
        result["trades"].to_csv(out / "trades_55day_v2.csv", index=False)
        result["equity"].to_csv(out / "equity_55day_v2.csv")
        print(f"  Trades → results/trades_55day_v2.csv")
        print(f"  Equity → results/equity_55day_v2.csv")
