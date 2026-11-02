import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Extra corporate-action repair on top of engine/bhav.py (read-only layer — the
Bhavcopy/_ohlcv files are NOT changed, so other projects are unaffected).

Why: the NSE register used by bhav.py misses some splits / bonuses and has no
special dividends or demergers at all. Each shows up as a fake overnight
crash (e.g. DAAWAT 1:10 split 7-Feb-2017, MAJESCO Rs 974 special dividend
23-Dec-2020, Orient Paper demerger Jan-2018).

Rule: NSE stocks have daily price bands, so an overnight move (prev close ->
today's open) below -45% or above +80% is treated as a corporate action.
All prices BEFORE that day are scaled by open/prev_close (volume inversely),
exactly how a split adjustment works. Real crashes can't move that much
between two sessions under the price bands.

    from backtesting.engine.bhav_fix import load      # drop-in for bhav.load
    .venv\Scripts\python.exe -m backtesting.engine.bhav_fix   # scan report
"""

import numpy as np
import pandas as pd

from backtesting.engine import bhav

DOWN, UP = -0.45, 0.80
MAX_GAP_DAYS = 30


def events(df: pd.DataFrame) -> pd.DataFrame:
    pc = df["close"].shift(1)
    jump = df["open"] / pc - 1
    hit = (jump < DOWN) | (jump > UP)
    ev = pd.DataFrame({"prev_close": pc[hit], "open": df["open"][hit], "factor": (df["open"] / pc)[hit]})
    gap_days = df.index.to_series().diff().dt.days
    ev["calendar_gap_days"] = gap_days[hit]
    return ev


def repair(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    ev = events(df)
    # after a suspension longer than a month a real loss is possible -> keep it
    ev = ev[ev["calendar_gap_days"].fillna(1) <= MAX_GAP_DAYS]
    for dt, f in ev["factor"].items():
        before = df.index < dt
        for c in ("open", "high", "low", "close"):
            df.loc[before, c] = df.loc[before, c] * f
        if "volume" in df:
            df.loc[before, "volume"] = df.loc[before, "volume"] / f
    return df


def load(sym: str) -> pd.DataFrame:
    df = bhav.load(sym)
    return repair(df[df["close"] > 0])


if __name__ == "__main__":
    from backtesting.book_strategies.schwager.stock_universe import stock_symbols
    rows = []
    for s in stock_symbols():
        try:
            df = bhav.load(s)
        except Exception:
            continue
        df = df[df["close"] > 0]
        e = events(df)
        for dt, r in e.iterrows():
            rows.append({"symbol": s, "date": dt.date(), "prev_close": r.prev_close, "open": r.open,
                         "move_pct": 100 * (r.factor - 1), "gap_days": r.calendar_gap_days,
                         "tradable": bool(df.loc[dt, "tradable"])})
    t = pd.DataFrame(rows).sort_values("date")
    t.to_csv(bhav.OUT.parent / "_matrix" / "detected_corporate_actions.csv", index=False)
    print(f"{len(t)} events in {t.symbol.nunique()} stocks "
          f"({(t.move_pct < 0).sum()} down, {(t.move_pct > 0).sum()} up; {t.tradable.sum()} while in top-500)")
    for s in ("DAAWAT", "BOROSIL", "MAJESCO", "ORIENTPPR", "EXCEL"):
        print(t[t.symbol == s].to_string(index=False, header=False))
    print(t.sample(15, random_state=1).to_string(index=False))
