"""
stitch_stocks.py
For all 16 stocks in the top-8 pairs:
  1. Download from Fyers if not already on disk (BRIGADE, JSWENERGY)
  2. Load Fyers 5-min data → resample to daily close
  3. Stitch with Yahoo Finance cache (2015-2024-05-27)
  4. Save as data/stocks/{SYM}_ext.parquet  (used by batch_backtest.py)
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import json
import time
from datetime import date, timedelta

import pandas as pd

from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv
from downloader.fetch_ohlcv import (
    get_date_chunks, get_parquet_path, load_existing, fetch_chunk
)
from config.settings import DATA_DIR

STOCK_DIR = PROJECT_ROOT / "backtesting/book_strategies/ernie_chan_qt/data/stocks"
YF_CUTOFF = pd.Timestamp("2024-05-27")
FYERS_START = date(2024, 5, 28)

# ── 16 stocks for the 8 PASS pairs ────────────────────────────────────────────
# (sym, fyers_symbol)
STOCKS = [
    ("HDFCBANK",   "NSE:HDFCBANK-EQ"),
    ("KOTAKBANK",  "NSE:KOTAKBANK-EQ"),
    ("HINDUNILVR", "NSE:HINDUNILVR-EQ"),
    ("DABUR",      "NSE:DABUR-EQ"),
    ("OBEROIRLTY", "NSE:OBEROIRLTY-EQ"),
    ("BRIGADE",    "NSE:BRIGADE-EQ"),        # needs download
    ("TATAPOWER",  "NSE:TATAPOWER-EQ"),
    ("JSWENERGY",  "NSE:JSWENERGY-EQ"),      # needs download
    ("TECHM",      "NSE:TECHM-EQ"),
    ("COFORGE",    "NSE:COFORGE-EQ"),
    ("ALKEM",      "NSE:ALKEM-EQ"),
    ("TORNTPHARM", "NSE:TORNTPHARM-EQ"),
    ("EICHERMOT",  "NSE:EICHERMOT-EQ"),
    ("TVSMOTORS",  "NSE:TVSMOTOR-EQ"),       # Fyers uses TVSMOTOR
    ("SHREECEM",   "NSE:SHREECEM-EQ"),
    ("RAMCOCEM",   "NSE:RAMCOCEM-EQ"),
]


def get_fyers_client():
    from fyers_apiv3 import fyersModel  # installed in system Python, not venv
    token_path = PROJECT_ROOT / "config/access_token.txt"
    data = json.loads(token_path.read_text())
    token = data["token"] if isinstance(data, dict) else data
    return fyersModel.FyersModel(
        client_id="W09OMXQB8J-100",
        is_async=False,
        token=token,
        log_path="",
    )


def download_symbol(fyers, fyers_sym: str, sym: str):
    """Download Fyers 5-min data for a symbol that isn't on disk yet."""
    end = date.today()
    chunks = get_date_chunks(FYERS_START, end, chunk_days=90)
    print(f"  Downloading {fyers_sym} — {len(chunks)} chunks...")
    all_dfs = []
    for i, (from_d, to_d) in enumerate(chunks, 1):
        chunk_df = fetch_chunk(fyers, fyers_sym, from_d, to_d)
        if chunk_df is not None and not chunk_df.empty:
            all_dfs.append(chunk_df)
            # Save chunk to parquet (grouped by year)
            for yr, grp in chunk_df.groupby(chunk_df["datetime"].dt.year):
                path = get_parquet_path(fyers_sym, yr)
                existing = pd.read_parquet(path) if path.exists() else pd.DataFrame()
                combined = pd.concat([existing, grp]).drop_duplicates(
                    subset=["datetime"]
                ).sort_values("datetime")
                combined.to_parquet(path, index=False)
        time.sleep(0.5)
    total = sum(len(d) for d in all_dfs)
    print(f"  {sym}: downloaded {total:,} 5-min bars")


def fyers_to_daily(fyers_sym: str, sym: str) -> pd.Series:
    """Load Fyers 5-min data from disk and resample to daily close."""
    loader = DataLoader()
    try:
        raw = loader.load(fyers_sym, start="2024-05-28")
    except Exception as e:
        print(f"  WARNING: DataLoader failed for {fyers_sym}: {e}")
        return pd.Series(dtype=float)
    if raw is None or raw.empty:
        print(f"  WARNING: No Fyers data for {fyers_sym}")
        return pd.Series(dtype=float)
    daily = resample_ohlcv(raw, "1D")
    daily.index = daily.index.normalize()
    s = daily["close"].rename(sym)
    s = s[s.index > YF_CUTOFF]
    return s.dropna()


def load_yf_cache(sym: str) -> pd.Series:
    """Load Yahoo Finance cached daily close."""
    cache = STOCK_DIR / f"{sym}_yf.parquet"
    if not cache.exists():
        print(f"  WARNING: No YF cache for {sym}")
        return pd.Series(dtype=float)
    raw = pd.read_parquet(cache)
    s = raw.iloc[:, 0] if isinstance(raw, pd.DataFrame) else raw
    s.index = pd.to_datetime(s.index).normalize()
    s = s.sort_index().dropna()
    return s[s.index <= YF_CUTOFF]


def stitch_and_save(sym: str, fyers_sym: str):
    yf_s   = load_yf_cache(sym)
    fy_s   = fyers_to_daily(fyers_sym, sym)

    if yf_s.empty and fy_s.empty:
        print(f"  {sym}: SKIP — no data from either source")
        return

    if yf_s.empty:
        combined = fy_s
    elif fy_s.empty:
        combined = yf_s
        print(f"  {sym}: WARNING — no Fyers data, using YF only")
    else:
        combined = pd.concat([yf_s, fy_s]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]

    combined = combined.dropna()
    out_path = STOCK_DIR / f"{sym}_ext.parquet"
    combined.to_frame(name=sym).to_parquet(out_path)

    last_yf  = yf_s.index[-1].date()  if not yf_s.empty  else "—"
    first_fy = fy_s.index[0].date()   if not fy_s.empty  else "—"
    last_fy  = fy_s.index[-1].date()  if not fy_s.empty  else "—"
    print(f"  {sym:<14} YF:{len(yf_s):>4}d (→{last_yf})  "
          f"Fyers:{len(fy_s):>4}d ({first_fy}→{last_fy})  "
          f"Total:{len(combined):>4}d  → saved {sym}_ext.parquet")


if __name__ == "__main__":
    SEP = "=" * 70
    print(f"\n{SEP}")
    print("  STITCH STOCKS  (Yahoo Finance 2015-2024  +  Fyers 2024-2026)")
    print(SEP)

    # ── Step 1: Download any missing Fyers stocks ──────────────────────────────
    missing = []
    for sym, fyers_sym in STOCKS:
        folder = DATA_DIR / fyers_sym.replace(":", "_").replace("-", "_")
        if not folder.exists():
            missing.append((sym, fyers_sym))

    if missing:
        print(f"\n  Missing Fyers data for: {[s for s,_ in missing]}")
        print("  Initialising Fyers client...")
        fyers = get_fyers_client()
        for sym, fyers_sym in missing:
            print(f"\n  Downloading {sym} ({fyers_sym})...")
            download_symbol(fyers, fyers_sym, sym)
    else:
        print("  All stocks already on disk — skipping download.")

    # ── Step 2: Stitch all 16 stocks ───────────────────────────────────────────
    print(f"\n{'─'*70}")
    print("  Stitching Yahoo Finance + Fyers for all 16 stocks...")
    print(f"{'─'*70}")
    for sym, fyers_sym in STOCKS:
        stitch_and_save(sym, fyers_sym)

    print(f"\n{SEP}")
    print("  DONE — run batch_backtest.py to get 2015-2026 results")
    print(SEP)
