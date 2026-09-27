import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Nasdaq 100 — 9/21 EMA Crossover Strategy (5-min bars)
======================================================
Long:  9 EMA crosses above 21 EMA → enter long, SL below crossover candle low
Short: 9 EMA crosses below 21 EMA → enter short, SL above crossover candle high
Ride the trend until the opposite crossover closes the position.

Data: Dukascopy 1-min bars resampled to 5-min.
Coverage: Sep 2011 → present (~15 years).
"""

import json
import logging
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

DATA_DIR = Path("G:/fyers_data_pipeline/forex/Dukascopy_1m/usatechidxusd")
OUTPUT_DIR = Path("G:/fyers_data_pipeline/forex/results")
OUTPUT_DIR.mkdir(exist_ok=True)

# ── Strategy parameters ──────────────────────────────────────────────
FAST_EMA = 9
SLOW_EMA = 21
INITIAL_CAPITAL = 100_000       # $100k
SLIPPAGE = 0.0001               # 0.01% (1 pip on indices ~ 0.1 point)
COMMISSION = 0.0002             # round-trip
STOP_OFFSET = 0                 # SL = crossover candle low (no extra buffer)


def load_5min(start_year: int = 2019, end_year: int = 2026) -> pd.DataFrame:
    """Load 1-min parquets, resample to 5-min OHLC."""
    files = []
    for y in range(start_year, end_year + 1):
        ydir = DATA_DIR / str(y)
        if not ydir.exists():
            continue
        for f in sorted(ydir.glob("ohlcv_1m_*.parquet")):
            files.append(f)

    if not files:
        raise FileNotFoundError(f"No data found in {DATA_DIR}")

    logger.info(f"Loading {len(files)} monthly files...")
    dfs = []
    for f in files:
        df = pd.read_parquet(f)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime").sort_index()
        dfs.append(df)

    raw = pd.concat(dfs).drop_duplicates().sort_index()
    logger.info(f"Raw 1-min bars: {len(raw):,}  ({raw.index[0]} → {raw.index[-1]})")

    # Resample to 5-min
    df5 = raw.resample("5min", closed="left", label="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).dropna(subset=["open", "close"])

    logger.info(f"5-min bars: {len(df5):,}  ({df5.index[0]} → {df5.index[-1]})")
    return df5


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=FAST_EMA, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=SLOW_EMA, adjust=False).mean()
    return df


def run_backtest(df: pd.DataFrame) -> dict:
    """Run the 9/21 crossover strategy."""
    df = add_indicators(df)
    df["position"] = 0        # +1 long, -1 short, 0 flat
    df["entry_price"] = np.nan
    df["sl_price"] = np.nan
    df["trade_pnl"] = 0.0

    trades = []
    capital = INITIAL_CAPITAL
    position = 0              # current position: 0, +1, -1
    entry_price = 0.0
    sl_price = 0.0

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    ema_fast = df["ema_fast"].values
    ema_slow = df["ema_slow"].values
    idx = df.index

    # Skip NaN EMA warmup
    start = SLOW_EMA + 1

    for i in range(start, len(df)):
        fast = ema_fast[i]
        slow = ema_slow[i]
        prev_fast = ema_fast[i - 1]
        prev_slow = ema_slow[i - 1]

        # Skip if EMAs are NaN
        if np.isnan(fast) or np.isnan(slow) or np.isnan(prev_fast) or np.isnan(prev_slow):
            continue

        price = closes[i]

        # ── LONG entry: fast crosses above slow ──
        if position == 0 and prev_fast <= prev_slow and fast > slow:
            position = 1
            entry_price = price * (1 + SLIPPAGE)
            sl_price = lows[i] - STOP_OFFSET

        # ── SHORT entry: fast crosses below slow ──
        elif position == 0 and prev_fast >= prev_slow and fast < slow:
            position = -1
            entry_price = price * (1 - SLIPPAGE)
            sl_price = highs[i] + STOP_OFFSET

        # ── Exit: opposite crossover ──
        elif position == 1 and prev_fast >= prev_slow and fast < slow:
            # Long exit, then go short
            pnl = (price * (1 - SLIPPAGE) - entry_price) / entry_price
            trades.append({"direction": "long", "entry": entry_price, "exit": price,
                           "pnl_pct": pnl, "bars_held": 0})
            capital *= (1 + pnl)
            # Enter short
            position = -1
            entry_price = price * (1 - SLIPPAGE)
            sl_price = highs[i] + STOP_OFFSET

        elif position == -1 and prev_fast <= prev_slow and fast > slow:
            # Short exit, then go long
            pnl = (entry_price - price * (1 + SLIPPAGE)) / entry_price
            trades.append({"direction": "short", "entry": entry_price, "exit": price,
                           "pnl_pct": pnl, "bars_held": 0})
            capital *= (1 + pnl)
            # Enter long
            position = 1
            entry_price = price * (1 + SLIPPAGE)
            sl_price = lows[i] - STOP_OFFSET

        # ── Stop loss check ──
        if position == 1 and lows[i] <= sl_price:
            pnl = (sl_price * (1 - SLIPPAGE) - entry_price) / entry_price
            trades.append({"direction": "long", "entry": entry_price, "exit": sl_price,
                           "pnl_pct": pnl, "bars_held": 0, "stopped": True})
            capital *= (1 + pnl)
            position = 0

        elif position == -1 and highs[i] >= sl_price:
            pnl = (entry_price - sl_price * (1 + SLIPPAGE)) / entry_price
            trades.append({"direction": "short", "entry": entry_price, "exit": sl_price,
                           "pnl_pct": pnl, "bars_held": 0, "stopped": True})
            capital *= (1 + pnl)
            position = 0

    # Close any open position at the end
    if position != 0:
        price = closes[-1]
        if position == 1:
            pnl = (price * (1 - SLIPPAGE) - entry_price) / entry_price
        else:
            pnl = (entry_price - price * (1 + SLIPPAGE)) / entry_price
        trades.append({"direction": "long" if position == 1 else "short",
                       "entry": entry_price, "exit": price, "pnl_pct": pnl,
                       "bars_held": 0, "closed_eod": True})
        capital *= (1 + pnl)

    return {"trades": trades, "final_capital": capital}


def analyse(trades: list) -> dict:
    df = pd.DataFrame(trades)
    if df.empty:
        return {"trades": 0, "total_pnl_pct": 0}

    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]

    total_pnl = (df["pnl_pct"].sum()) * 100
    win_rate = len(wins) / len(df) * 100
    avg_win = wins["pnl_pct"].mean() * 100 if len(wins) else 0
    avg_loss = losses["pnl_pct"].mean() * 100 if len(losses) else 0
    profit_factor = abs(wins["pnl_pct"].sum() / losses["pnl_pct"].sum()) if len(losses) and losses["pnl_pct"].sum() != 0 else float("inf")
    stopped = df.get("stopped", pd.Series([False] * len(df))).sum()

    # Max consecutive wins/losses
    results = (df["pnl_pct"] > 0).astype(int).values
    max_consec_wins = max_consec = 0
    max_consec_losses = consec = 0
    for r in results:
        if r:
            consec += 1
            max_consec = max(max_consec, consec)
            max_consec_losses = 0
        else:
            consec = 0
            max_consec_losses = max(max_consec_losses, consec + 1)
            max_consec = 0

    return {
        "trades": len(df),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl_pct": round(total_pnl, 2),
        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "profit_factor": round(profit_factor, 2),
        "stopped": int(stopped),
        "max_consec_wins": int(max_consec),
        "max_consec_losses": int(max_consec_losses),
    }


def main():
    df = load_5min(start_year=2019, end_year=2026)
    result = run_backtest(df)
    stats = analyse(result["trades"])

    print(f"\n{'='*60}")
    print(f"  Nasdaq 100 CFD — 9/21 EMA Crossover (5-min)")
    print(f"  Period: {df.index[0].date()} → {df.index[-1].date()}")
    print(f"{'='*60}")
    print(f"  Initial capital:    ${INITIAL_CAPITAL:>12,.0f}")
    print(f"  Final capital:      ${result['final_capital']:>12,.0f}")
    print(f"  Net P&L:              {stats['total_pnl_pct']:>11.2f}%")
    print(f"  Total trades:        {stats['trades']:>12,}")
    print(f"  Wins / Losses:       {stats['wins']:>6,} / {stats['losses']:>6,}")
    print(f"  Win rate:            {stats['win_rate']:>11.1f}%")
    print(f"  Avg win:             {stats['avg_win']:>11.3f}%")
    print(f"  Avg loss:            {stats['avg_loss']:>11.3f}%")
    print(f"  Profit factor:       {stats['profit_factor']:>11.2f}")
    print(f"  Stopped trades:      {stats['stopped']:>12,}")
    print(f"  Max consec wins:     {stats['max_consec_wins']:>12,}")
    print(f"  Max consec losses:   {stats['max_consec_losses']:>12,}")
    print(f"{'='*60}\n")

    # Save results
    out_csv = OUTPUT_DIR / "nasdaq_9_21_ema_results.csv"
    pd.DataFrame(result["trades"]).to_csv(out_csv, index=False)
    logger.info(f"Trade log saved: {out_csv}")

    # Save summary
    out_json = OUTPUT_DIR / "nasdaq_9_21_ema_summary.json"
    out_json.write_text(json.dumps({"stats": stats, "final_capital": round(result["final_capital"], 2),
                                     "period_start": str(df.index[0].date()),
                                     "period_end": str(df.index[-1].date())}, indent=2))
    logger.info(f"Summary saved: {out_json}")


if __name__ == "__main__":
    main()
