import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Multi-instrument 1-minute downloader (Dukascopy)
===================================================
XAUUSD alone caps the portfolio's CAGR/DD ratio at ~0.47 — nine strategies on
one market are ultimately all betting on one price series. This downloads a
basket of genuinely different markets so the same validated pipeline can be
run across asset classes, which is how a ratio above 1.0 becomes reachable.

1-MINUTE resolution (not 1-second): every surviving strategy trades on 1h-1D
bars, so 1m is ample and roughly 60x smaller to store and load.

Output: H:\\My Drive\\Dukascopy_1m\\{instrument}\\{year}\\ohlcv_1m_{MM}.parquet
Manifest per instrument so an interrupted run resumes exactly where it stopped.

Run: python forex/download_basket_1m.py
"""

import json
import subprocess
import time
import logging
import calendar
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).parent.parent))
from config.settings import PARQUET_COMPRESSION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler("logs/basket_download.log", encoding="utf-8")],
)
logger = logging.getLogger(__name__)

FOREX_DIR   = Path(__file__).parent
STAGING_DIR = FOREX_DIR / "_staging_basket"
OUTPUT_ROOT = Path("H:/My Drive/Dukascopy_1m")
MANIFEST    = FOREX_DIR / "basket_manifest.json"

TIMEFRAME = "m1"
PRICE_TYPE = "bid"
START_YEAR, START_MONTH = 2003, 1        # aligns with the XAUUSD history
SLEEP_BETWEEN = 2
MAX_RETRIES = 3

# Deliberately spread across asset classes — FX, metals, energy, equity indices.
# (id, label, earliest data available per Dukascopy)
BASKET = [
    # Equity indices FIRST (user request 2026-08-22).
    # ⚠️ These are Dukascopy CFDs, NOT exchange futures. Dukascopy is the
    # counterparty and states its CFD prices are "neither legally neither
    # economically the derivative from the hedging instrument" and are not a
    # propagation of exchange prices. Use for research only — any strategy
    # must be re-validated on the instrument actually traded (e.g. NSE Nifty
    # futures) before it means anything live.
    ("usa500idxusd",  "S&P 500 (CFD)",   1980),
    ("usatechidxusd", "Nasdaq 100 (CFD)",1990),
    # Energy — MCX Crude Oil is the domestic equivalent
    ("lightcmdusd",   "US Light Crude",  1983),
    ("brentcmdusd",   "US Brent Crude",  2006),
    # Metals — MCX Silver is the domestic equivalent
    ("xagusd",        "Silver",          1999),
    # FX majors — NSE currency futures are the domestic equivalent
    ("eurusd",        "EUR/USD",         1973),
    ("gbpusd",        "GBP/USD",         1986),
    ("usdjpy",        "USD/JPY",         1986),
    ("audusd",        "AUD/USD",         1993),
    ("usdchf",        "USD/CHF",         1986),
    ("usdcad",        "USD/CAD",         1986),
]


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}


def save_manifest(m: dict):
    MANIFEST.write_text(json.dumps(m, indent=2))


def month_range(sy, sm):
    today = date.today()
    y, m = sy, sm
    while (y, m) <= (today.year, today.month):
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1


def out_path(inst: str, y: int, m: int) -> Path:
    folder = OUTPUT_ROOT / inst / f"{y:04d}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"ohlcv_1m_{m:02d}.parquet"


def fetch_month(inst: str, y: int, m: int) -> Path | None:
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)   # -to is EXCLUSIVE
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{inst}_{y:04d}-{m:02d}"

    cmd = ["npx", "dukascopy-node", "-i", inst,
           "-from", f"{y:04d}-{m:02d}-01", "-to", f"{ny:04d}-{nm:02d}-01",
           "-t", TIMEFRAME, "-p", PRICE_TYPE, "-f", "csv",
           "-dir", str(STAGING_DIR), "-fn", fname, "-r", "3", "-fr"]

    r = subprocess.run(cmd, cwd=str(FOREX_DIR), shell=True,
                       capture_output=True, text=True, timeout=900)
    csv = STAGING_DIR / f"{fname}.csv"
    if r.returncode != 0 or not csv.exists():
        return None
    return csv


def convert(csv: Path, inst: str, y: int, m: int) -> int:
    if csv.stat().st_size == 0:
        csv.unlink(missing_ok=True)
        return 0
    df = pd.read_csv(csv)
    if df.empty:
        csv.unlink(missing_ok=True)
        return 0
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.drop(columns=["timestamp"])
    df = df[["datetime", "open", "high", "low", "close"]].sort_values("datetime")
    df = df[(df["datetime"].dt.year == y) & (df["datetime"].dt.month == m)]
    if df.empty:
        csv.unlink(missing_ok=True)
        return 0
    df.to_parquet(out_path(inst, y, m), compression=PARQUET_COMPRESSION, index=False)
    csv.unlink(missing_ok=True)
    return len(df)


def main():
    if not OUTPUT_ROOT.parent.exists():
        logger.error(f"Google Drive not mounted: {OUTPUT_ROOT.parent}")
        sys.exit(1)

    man = load_manifest()
    months = list(month_range(START_YEAR, START_MONTH))
    logger.info(f"Basket: {len(BASKET)} instruments x {len(months)} months, 1-minute bars")

    for inst, label, earliest in BASKET:
        man.setdefault(inst, {})
        done = sum(1 for v in man[inst].values() if v.get("status") == "success")
        logger.info(f"=== {label} ({inst}) — {done} months already stored ===")
        rows_total = 0

        for y, m in months:
            key = f"{y:04d}-{m:02d}"
            if man[inst].get(key, {}).get("status") in ("success", "no_data"):
                continue
            if y < earliest:
                man[inst][key] = {"status": "no_data", "rows": 0}
                continue

            csv = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    csv = fetch_month(inst, y, m)
                except Exception as e:
                    logger.warning(f"{inst} {key} attempt {attempt}: {e}")
                if csv is not None:
                    break
                time.sleep(5 * attempt)

            if csv is None:
                man[inst][key] = {"status": "failed", "rows": 0}
            else:
                try:
                    n = convert(csv, inst, y, m)
                except Exception as e:
                    logger.error(f"{inst} {key} convert failed: {e}")
                    n = 0
                man[inst][key] = {"status": "success" if n else "no_data", "rows": n}
                rows_total += n

            save_manifest(man)
            time.sleep(SLEEP_BETWEEN)

        logger.info(f"{label}: +{rows_total:,} rows this run")

    logger.info("Basket download complete")
    for inst, label, _ in BASKET:
        tot = sum(v.get("rows", 0) for v in man.get(inst, {}).values())
        ok  = sum(1 for v in man.get(inst, {}).values() if v.get("status") == "success")
        logger.info(f"  {label:<16} {ok:>4} months  {tot:>12,} rows")


if __name__ == "__main__":
    main()
