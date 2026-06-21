import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
NIFTY Options Daily Capture Pipeline
=====================================
Fyers only serves data for ACTIVE (not yet expired) option contracts.
This script must run daily to capture each contract's data before expiry.

Strategy:
  1. Download Fyers symbol master to get all currently-active NIFTY contracts
  2. Filter to ATM ± N strikes for each expiry
  3. Fetch 1-min OHLCV from start-of-contract to today (incremental)
  4. Save to parquet — data accumulates over time as contracts are captured

Run daily via daily_update.bat (add after equity pipeline).
"""

import argparse
import logging
import time
import io
import requests
import pandas as pd
from datetime import date, timedelta
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from auth.fyers_auth import get_fyers_client
from options.fetch_options import download_contract, get_contract_path
import options.manifest as mfst
from config.settings import SLEEP_BETWEEN_CALLS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/options.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

SYMBOL_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"


def download_symbol_master() -> pd.DataFrame:
    """Fetch the Fyers F&O symbol master CSV."""
    r = requests.get(SYMBOL_MASTER_URL, timeout=20)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), header=None, low_memory=False)
    # col 9 = Fyers symbol,  col 1 = description,  col 15 = strike price
    df.columns = list(range(len(df.columns)))
    return df


def get_active_nifty_options(master: pd.DataFrame, current_price: float,
                              n_strikes: int, max_expiries: int) -> pd.DataFrame:
    """
    Filter symbol master to ATM ± n_strikes NIFTY options for the nearest
    max_expiries weekly expiry dates.

    Returns DataFrame with columns: [symbol, description, expiry_str, strike, type]
    """
    # NIFTY options: description starts with "NIFTY " followed by a digit (day)
    nifty = master[
        master[1].astype(str).str.match(r"^NIFTY \d") &
        master[1].astype(str).str.contains(r" CE| PE")
    ].copy()

    nifty["symbol"]      = nifty[9].astype(str)
    nifty["description"] = nifty[1].astype(str)
    nifty["strike"]      = pd.to_numeric(nifty[15], errors="coerce")
    nifty["opt_type"]    = nifty[1].astype(str).str[-2:]  # last 2 chars of description

    # Parse expiry string from description: "NIFTY DD Mon YY STRIKE CE/PE"
    def parse_expiry(desc):
        parts = desc.split()
        if len(parts) >= 4:
            return f"{parts[1]} {parts[2]} {parts[3]}"
        return ""

    nifty["expiry_str"] = nifty["description"].apply(parse_expiry)

    # Get nearest max_expiries unique expiry dates
    unique_expiries = sorted(nifty["expiry_str"].unique())[:max_expiries]
    nifty = nifty[nifty["expiry_str"].isin(unique_expiries)]

    # Filter to ATM ± n_strikes
    atm = round(current_price / 50) * 50
    low  = atm - n_strikes * 50
    high = atm + n_strikes * 50
    nifty = nifty[(nifty["strike"] >= low) & (nifty["strike"] <= high)]

    return nifty[["symbol", "description", "expiry_str", "strike", "opt_type"]].reset_index(drop=True)


def parse_expiry_date(expiry_str: str) -> date | None:
    """Parse 'DD Mon YY' → date. e.g. '02 Jun 26' → date(2026, 6, 2)"""
    try:
        return pd.to_datetime(expiry_str, format="%d %b %y").date()
    except Exception:
        return None


def run_pipeline(n_strikes: int = 10, max_expiries: int = 4,
                 days_lookback: int = 30):
    today = date.today()

    print(f"\n{'='*60}")
    print("  NIFTY OPTIONS DAILY CAPTURE PIPELINE")
    print(f"{'='*60}")
    print(f"  Date           : {today}")
    print(f"  ATM strikes    : +/- {n_strikes} ({2*n_strikes+1} strikes per expiry)")
    print(f"  Expiries       : next {max_expiries} weekly expiries")
    print(f"  History window : up to {days_lookback} days before expiry")
    print(f"{'='*60}\n")

    fyers = get_fyers_client()

    # ── Step 1: Get current NIFTY price ────────────────────────
    r = fyers.quotes({"symbols": "NSE:NIFTY50-INDEX"})
    if r.get("s") != "ok":
        raise RuntimeError(f"Failed to get NIFTY quote: {r}")
    current_price = float(r["d"][0]["v"]["lp"])
    atm = round(current_price / 50) * 50
    print(f"NIFTY current price: {current_price:,.2f}  ATM: {atm:,}\n")

    # ── Step 2: Load symbol master ──────────────────────────────
    print("Downloading Fyers symbol master...")
    master = download_symbol_master()
    print(f"Symbol master: {len(master):,} rows\n")

    # ── Step 3: Filter to active NIFTY options ──────────────────
    contracts = get_active_nifty_options(master, current_price, n_strikes, max_expiries)
    print(f"Active NIFTY contracts to fetch: {len(contracts)}")

    expiries = contracts["expiry_str"].unique()
    for exp in expiries:
        n = len(contracts[contracts["expiry_str"] == exp])
        print(f"  {exp}: {n} contracts")
    print()

    # ── Step 4: Download loop ────────────────────────────────────
    man = mfst.load_manifest()
    total = downloaded = skipped = no_data_count = errors = 0

    for _, row in contracts.iterrows():
        symbol    = row["symbol"]
        expiry    = parse_expiry_date(row["expiry_str"])
        strike    = int(row["strike"])
        opt_type  = row["opt_type"].strip()

        if expiry is None:
            errors += 1
            continue

        # Fetch from (expiry - days_lookback) to today
        from_date = expiry - timedelta(days=days_lookback)
        total += 1

        # Skip if parquet already exists AND was updated today (incremental check)
        path = get_contract_path(expiry, strike, opt_type)
        if path.exists():
            # Check if already updated today
            mtime = date.fromtimestamp(path.stat().st_mtime)
            if mtime >= today:
                skipped += 1
                continue

        result = download_contract(fyers, symbol, expiry, strike, opt_type, from_date)
        mfst.mark_fetched(man, expiry, strike, opt_type, result)
        mfst.save_manifest(man)

        status = result["status"]
        if status == "success":
            downloaded += 1
            print(f"  {symbol}: {result['bars']} bars  ({result['date_from']} to {result['date_to']})")
        elif status == "no_data":
            no_data_count += 1
        elif status == "up_to_date":
            skipped += 1
        else:
            errors += 1
            logger.warning(f"  {symbol}: status={status}")

        time.sleep(SLEEP_BETWEEN_CALLS)

    # ── Summary ──────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  DONE")
    print(f"{'='*60}")
    print(f"  Total contracts : {total}")
    print(f"  Downloaded      : {downloaded}")
    print(f"  Skipped (fresh) : {skipped}")
    print(f"  No data         : {no_data_count}")
    print(f"  Errors          : {errors}")
    mfst.print_summary(man)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NIFTY Options Daily Capture — run daily to build historical options data")
    parser.add_argument("--strikes",      type=int, default=10,
                        help="Strikes each side of ATM (default: 10)")
    parser.add_argument("--expiries",     type=int, default=4,
                        help="Number of upcoming expiries to capture (default: 4)")
    parser.add_argument("--days-lookback", type=int, default=30,
                        help="Days before expiry to fetch from (default: 30)")
    args = parser.parse_args()

    run_pipeline(n_strikes=args.strikes, max_expiries=args.expiries,
                 days_lookback=args.days_lookback)
