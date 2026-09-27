import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Download Nasdaq 100 1-minute bars from Dukascopy to local G: drive.
Resumes from the existing basket_manifest.json (skips already-downloaded months).
"""

import json
import subprocess
import time
import logging
from pathlib import Path
from datetime import date

import pandas as pd

sys.path.append(str(Path(__file__).parent))
from download_basket_1m import month_range, fetch_month, save_manifest, load_manifest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

INSTRUMENT = "usatechidxusd"
OUTPUT_ROOT = Path("G:/fyers_data_pipeline/forex/Dukascopy_1m/usatechidxusd")
MANIFEST = Path("G:/fyers_data_pipeline/forex/basket_manifest.json")
TIMEFRAME = "m1"
PRICE_TYPE = "bid"
SLEEP_BETWEEN = 1
MAX_RETRIES = 3
START_YEAR, START_MONTH = 2003, 1


def out_path(y: int, m: int) -> Path:
    folder = OUTPUT_ROOT / f"{y:04d}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"ohlcv_1m_{m:02d}.parquet"


def convert(csv: Path, y: int, m: int) -> int:
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
    df.to_parquet(out_path(y, m), compression="snappy", index=False)
    csv.unlink(missing_ok=True)
    return len(df)


def main():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    man = load_manifest()
    months = list(month_range(START_YEAR, START_MONTH))

    done = sum(1 for v in man[INSTRUMENT].values() if v.get("status") == "success")
    logger.info(f"Nasdaq 100 CFD ({INSTRUMENT}) — {done} months already stored, resuming...")

    rows_total = 0
    for y, m in months:
        key = f"{y:04d}-{m:02d}"
        status = man[INSTRUMENT].get(key, {}).get("status")
        if status == "success":
            continue
        if status == "no_data":
            continue

        csv = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                csv = fetch_month(INSTRUMENT, y, m)
            except Exception as e:
                logger.warning(f"{key} attempt {attempt}: {e}")
            if csv is not None:
                break
            time.sleep(5 * attempt)

        if csv is None:
            man[INSTRUMENT][key] = {"status": "failed", "rows": 0}
            logger.info(f"{key}: failed after {MAX_RETRIES} attempts")
        else:
            try:
                n = convert(csv, y, m)
            except Exception as e:
                logger.error(f"{key} convert failed: {e}")
                n = 0
            man[INSTRUMENT][key] = {"status": "success" if n else "no_data", "rows": n}
            rows_total += n
            logger.info(f"{key}: +{n:,} rows")

        save_manifest(man)
        time.sleep(SLEEP_BETWEEN)

    # Final summary
    ok = sum(1 for v in man[INSTRUMENT].values() if v.get("status") == "success")
    nd = sum(1 for v in man[INSTRUMENT].values() if v.get("status") == "no_data")
    tot_rows = sum(v.get("rows", 0) for v in man[INSTRUMENT].values())
    logger.info(f"Done: {ok} success, {nd} no_data, {tot_rows:,} total rows")


if __name__ == "__main__":
    main()
