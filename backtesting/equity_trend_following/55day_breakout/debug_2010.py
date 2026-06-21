import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
from pathlib import Path
from strategy_55day_breakout import load_symbol, compute_signals, DATA_DIR, STARTING_CAPITAL, MAX_POSITIONS

POSITION_PCT  = 0.01
HWM_DD_STOP   = 0.15
NIFTY_MA_PERIOD = 200

def load_nifty(path):
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    return df["close"] > df["close"].rolling(NIFTY_MA_PERIOD).mean()

def get_trail_exit_price(df, date):
    idx = df.index.searchsorted(date)
    if idx < len(df) and df.index[idx] == date:
        next_idx = idx + 1
        return df.iloc[next_idx]["open"] if next_idx < len(df) else df.iloc[idx]["close"]
    elif idx < len(df):
        return df.iloc[idx]["open"]
    return df.iloc[-1]["close"]

symbols = [p.stem for p in DATA_DIR.glob("*.parquet")]
all_data = {}
for sym in symbols:
    df = load_symbol(sym)
    if df is None: continue
    df = compute_signals(df)
    if len(df) >= 10:
        all_data[sym] = df

all_dates   = sorted(set().union(*[set(df.index) for df in all_data.values()]))
nifty_above = load_nifty(Path(__file__).parent / "nifty_daily.parquet")

open_positions = {}
cash = STARTING_CAPITAL
peak_equity = STARTING_CAPITAL

# Run from start, print daily snapshot around the 2010 event
DEBUG_START = pd.Timestamp("2009-12-01")
DEBUG_END   = pd.Timestamp("2010-02-28")

for date in all_dates:
    open_value = sum(
        pos["shares"] * all_data[sym].loc[date]["close"]
        for sym, pos in open_positions.items()
        if sym in all_data and date in all_data[sym].index
    )
    current_equity = cash + open_value
    if current_equity > peak_equity:
        peak_equity = current_equity

    dd = (current_equity - peak_equity) / peak_equity * 100

    # Trail exits
    to_exit = [
        sym for sym, pos in open_positions.items()
        if all_data.get(sym) is not None and date in all_data[sym].index
        and all_data[sym].loc[date]["exit_signal"]
    ]
    exits_today = len(to_exit)
    for sym in to_exit:
        pos = open_positions.pop(sym)
        exit_price = get_trail_exit_price(all_data[sym], date)
        cash += pos["shares"] * exit_price

    # Entries
    if bool(nifty_above.get(date, False)):
        slots_free = MAX_POSITIONS - len(open_positions)
        if slots_free > 0:
            ov2 = sum(pos["shares"] * all_data[sym].loc[date]["close"]
                      for sym, pos in open_positions.items()
                      if sym in all_data and date in all_data[sym].index)
            pos_size = (cash + ov2) * POSITION_PCT
            candidates = [
                (sym, df) for sym, df in all_data.items()
                if sym not in open_positions and date in df.index and df.loc[date]["entry_signal"]
            ]
            for sym, df in candidates[:slots_free]:
                idx = df.index.searchsorted(date)
                next_idx = idx + 1
                if next_idx >= len(df): continue
                ep = df.iloc[next_idx]["open"]
                if ep <= 0: continue
                shares = int(pos_size / ep)
                if shares == 0 or shares * ep > cash: continue
                cash -= shares * ep
                open_positions[sym] = {"shares": shares, "entry_price": ep, "entry_date": df.index[next_idx], "position_size": shares*ep}

    # Debug print
    if DEBUG_START <= date <= DEBUG_END:
        ov_end = sum(pos["shares"] * all_data[sym].loc[date]["close"]
                     for sym, pos in open_positions.items()
                     if sym in all_data and date in all_data[sym].index)
        eq_end = cash + ov_end
        dd_end = (eq_end - peak_equity) / peak_equity * 100
        flag = " *** HWM BREACH ***" if dd <= -15 else ""
        print(f"{str(date)[:10]}  eq={eq_end/1e5:>7.2f}L  peak={peak_equity/1e5:>7.2f}L  dd={dd_end:>7.2f}%  pos={len(open_positions):>3}  exits={exits_today}{flag}")
