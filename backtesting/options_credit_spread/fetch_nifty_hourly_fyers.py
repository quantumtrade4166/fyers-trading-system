"""True 1-hour NIFTY 50 index candles from Fyers, for the Supertrend signal.

The signal used to be built from 1-minute spot closes embedded in the options
data, so each hour's high/low was the highest/lowest minute CLOSE rather than
the real traded extreme. Supertrend is driven by ATR (high-low ranges), so the
compressed ranges produced extra whipsaw flips that TradingView never shows.

Fyers hourly bars are anchored at 09:15 like TradingView's (09:15, 10:15 ...
15:15 stub). They are stamped with their OPEN time; here they are relabelled
by CLOSE time (15:15 stub -> 15:30) to match supertrend_signals.py, so a flip
is only actionable once its candle has finished.

Token: copy it from the VPS first (fetch_fyers_token_VPS.bat) — never generate
one locally, that kills the VPS feed.

    python -m backtesting.options_credit_spread.fetch_nifty_hourly_fyers
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import json
import time
from pathlib import Path

import pandas as pd
from fyers_apiv3 import fyersModel

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "NSE_NIFTY50_INDEX" / "ohlcv_60min.parquet"
SYMBOL = "NSE:NIFTY50-INDEX"
CHUNK_DAYS = 90          # Fyers caps intraday history at 100 days per request


def fetch(start: str, end: str) -> pd.DataFrame:
    tok = json.loads((ROOT / "config" / "access_token.txt").read_text(encoding="utf-8"))["token"]
    fy = fyersModel.FyersModel(client_id="W09OMXQB8J-100", is_async=False, token=tok, log_path="")

    frames = []
    cur, stop = pd.Timestamp(start), pd.Timestamp(end)
    while cur <= stop:
        to = min(cur + pd.Timedelta(days=CHUNK_DAYS - 1), stop)
        r = fy.history({"symbol": SYMBOL, "resolution": "60", "date_format": "1",
                        "range_from": cur.strftime("%Y-%m-%d"),
                        "range_to": to.strftime("%Y-%m-%d"), "cont_flag": "1"})
        if r.get("s") != "ok":
            raise RuntimeError(f"Fyers refused {cur.date()}..{to.date()}: {r}")
        candles = r.get("candles") or []
        print(f"  {cur.date()} -> {to.date()}  {len(candles):>4} bars")
        if candles:
            frames.append(pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"]))
        cur = to + pd.Timedelta(days=1)
        time.sleep(0.3)

    df = pd.concat(frames, ignore_index=True)
    opened = (pd.to_datetime(df["ts"], unit="s", utc=True)
              .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
    closed = opened + pd.Timedelta(hours=1)
    closed = closed.where(opened.dt.strftime("%H:%M") != "15:15",
                          opened.dt.normalize() + pd.Timedelta(hours=15, minutes=30))
    df["datetime"] = closed
    df = (df[["datetime", "open", "high", "low", "close"]]
          .drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    per_day = df.groupby(df["datetime"].dt.date).size()
    print(f"\nSaved {len(df):,} bars -> {OUT}")
    print(f"Range {df['datetime'].min()} -> {df['datetime'].max()} | {len(per_day)} days | "
          f"days without 7 bars: {(per_day != 7).sum()}")
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    # starts before the options data so Supertrend(10) is warmed up by Jan 2021
    ap.add_argument("--start", default="2020-10-01")
    ap.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    a = ap.parse_args()
    fetch(a.start, a.end)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
