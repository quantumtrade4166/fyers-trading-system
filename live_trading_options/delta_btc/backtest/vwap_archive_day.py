"""
backtest/vwap_archive_day.py — replay one settlement day from OUR chain archive.
================================================================================

The downloaded history (Bitcoin options data/) has no bid/ask. Our own collector
(data/chain_archive/{date}/chunk_*.parquet) does: one whole-chain snapshot a
minute with mark, best bid, best ask and 24h volume. For recent days that makes a
better replay than the history — fills are at the REAL top of book.

    .venv\Scripts\python.exe live_trading_options\delta_btc\backtest\vwap_archive_day.py --day 2026-09-19

Prints ist_day (strict <=100 and with the farthest-pair fallback) and full_expiry.

Approximations, all stated:
  - one snapshot a minute, so each minute is one candle point (live samples every
    2 s); candle highs/lows are therefore a touch tighter than live
  - per-minute volume = increase in the contract's 24h volume. A daily contract
    lists at 17:30 the day before and our window ends 17:10, inside 24h of
    listing, so nothing has rolled out of the window yet: the difference is the
    exact volume traded that minute
  - fills at top of book (the depth walk cannot be redone); the live 5% bad-fill
    guard is applied against that
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import glob
import argparse
import datetime as dt

import pandas as pd

from vwap_history import run_cycle


def load_archive_day(day: str) -> pd.DataFrame:
    D = dt.date.fromisoformat(day)
    code = f"{D.day:02d}{D.month:02d}{D.year % 100:02d}"
    frames = []
    for d in (D - dt.timedelta(days=1), D):
        for f in sorted(glob.glob(str(ROOT / "data" / "chain_archive" / d.isoformat() / "chunk_*.parquet"))):
            try:
                x = pd.read_parquet(f)
            except Exception:
                continue
            x = x[x["symbol"].astype(str).str.endswith(code)]
            if len(x):
                frames.append(x)
    if not frames:
        raise SystemExit(f"no archived rows for contracts settling {day}")
    df = pd.concat(frames)
    df["time_ist"] = pd.to_datetime(df["captured"]).dt.floor("min")
    df = df.sort_values("captured").drop_duplicates(["symbol", "time_ist"], keep="last")
    df = df.rename(columns={"best_bid": "bid", "best_ask": "ask"})
    df["mark_open"] = df["mark"]
    df["volume"] = (df.groupby("symbol")["volume"].diff().clip(lower=0)).fillna(0.0)
    df["bid"] = df["bid"].fillna(df["mark"])
    df["ask"] = df["ask"].fillna(df["mark"])
    return df[["time_ist", "symbol", "opt_type", "strike", "mark", "mark_open",
               "bid", "ask", "volume", "spot"]].dropna(subset=["mark", "spot"])


def show(title, r):
    print(f"\n── {title}")
    if r.get("skip"):
        print(f"   not traded: {r['skip']}")
        return
    print(f"   strikes {r['ce']:.0f} CE / {r['pe']:.0f} PE, picked {r['selected_at'][5:16]} "
          f"at {r['combined_at_select']:.2f}" +
          ("  (nothing <=100 — farthest equidistant pair)" if r["over_threshold"] else ""))
    for t in r["trades"]:
        print(f"   #{t['n']} {t['entry_time'][11:16]} sold {t['entry_combined']:7.2f} "
              f"(trig {t['trigger']:.2f}) -> {t['exit_time'][11:16]} bought "
              f"{t['exit_combined']:7.2f}   net ${t['net_usd']:+8.2f}  {t['exit_reason']}")
    print(f"   = {len(r['trades'])} trades, fees ${r['fees']:.2f}, NET ${r['net']:+.2f}"
          f"  (≈ ₹{r['net'] * 88.5:+,.0f})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    a = ap.parse_args()
    df = load_archive_day(a.day)
    print(f"archive rows for contracts settling {a.day}: {len(df):,} "
          f"({df['time_ist'].min()} -> {df['time_ist'].max()})")
    show("IST day, strict <=100 (the live rule)", run_cycle("ist_day", a.day, df, fallback=False, real_book=True))
    show("IST day, farthest pair if nothing <=100", run_cycle("ist_day", a.day, df, fallback=True, real_book=True))
    show("Full expiry (as live)", run_cycle("full_expiry", a.day, df, real_book=True))


if __name__ == "__main__":
    main()
