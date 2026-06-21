# ============================================================
# run_pipeline.py
# Main entry point for the data ingestion pipeline.
#
# USAGE:
#   Full initial download (first time):
#     python run_pipeline.py --mode full
#
#   Daily incremental update (run every day after market close):
#     python run_pipeline.py --mode update
#
#   Sync local data to Google Drive:
#     python run_pipeline.py --mode sync
#
#   Full download + sync in one shot:
#     python run_pipeline.py --mode full --sync
#
#   Check what data you have:
#     python run_pipeline.py --mode status
# ============================================================

import argparse
import logging
import time
import sys
from pathlib import Path
from datetime import datetime

# Fix Windows encoding issue
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

sys.path.append(str(Path(__file__).parent))

from config.settings import LOG_FILE, LOG_LEVEL, SLEEP_BETWEEN_SYMBOLS
from config.symbols import ALL_SYMBOLS
from auth.fyers_auth import get_fyers_client
from downloader.fetch_ohlcv import download_symbol
from tracker.manifest import (
    update_manifest_for_symbol,
    print_manifest_summary,
    rebuild_manifest_from_disk,
)


# ── Logging setup ────────────────────────────────────────────

def setup_logging():
    LOG_FILE.parent.mkdir(exist_ok=True)
    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format=fmt,
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )

logger = logging.getLogger(__name__)


# ── Pipeline modes ───────────────────────────────────────────

def run_download(fyers_client, symbols: list, force_full: bool = False):
    """
    Download data for all symbols.
    force_full=True  -> re-download everything from scratch
    force_full=False -> incremental (only missing dates)
    """
    total = len(symbols)
    results = {"success": 0, "up_to_date": 0, "no_data": 0, "failed": 0}
    changed_symbols = []

    mode_label = "FULL" if force_full else "INCREMENTAL"
    logger.info(f"Starting {mode_label} download for {total} symbols")
    start_time = time.time()

    for i, symbol in enumerate(symbols, 1):
        logger.info(f"[{i}/{total}] Processing {symbol}")
        try:
            result = download_symbol(fyers_client, symbol, force_full=force_full)
            update_manifest_for_symbol(symbol, result)
            status = result.get("status", "failed")
            results[status] = results.get(status, 0) + 1

            if status == "success":
                changed_symbols.append(symbol)
                logger.info(
                    f"  OK {symbol}: {result.get('bars', 0)} bars "
                    f"({result.get('date_from')} to {result.get('date_to')})"
                )
            elif status == "up_to_date":
                logger.info(f"  OK {symbol}: already up to date")
            else:
                logger.warning(f"  FAILED {symbol}: {status}")

        except Exception as e:
            logger.error(f"  ERROR {symbol}: unexpected error - {e}")
            results["failed"] += 1

        time.sleep(SLEEP_BETWEEN_SYMBOLS)

    elapsed = time.time() - start_time
    logger.info(
        f"\nDownload complete in {elapsed/60:.1f} min | "
        f"Success: {results['success']} | "
        f"Up-to-date: {results['up_to_date']} | "
        f"No data: {results['no_data']} | "
        f"Failed: {results['failed']}"
    )
    return changed_symbols


def run_sync(changed_symbols: list = None):
    """Sync local Parquet files to Google Drive."""
    try:
        from storage.gdrive_sync import sync_to_drive
        sync_to_drive(changed_symbols)
    except ImportError as e:
        logger.error(f"Drive sync failed (missing library): {e}")
    except FileNotFoundError as e:
        logger.error(f"Drive sync failed (credentials): {e}")
    except Exception as e:
        logger.error(f"Drive sync failed: {e}")


# ── Main ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nifty F&O Data Pipeline")
    parser.add_argument(
        "--mode",
        choices=["full", "update", "sync", "status", "rebuild"],
        default="update",
        help=(
            "full=download all history | "
            "update=incremental (missing data only) | "
            "sync=push to Google Drive | "
            "status=show manifest | "
            "rebuild=rebuild manifest from disk"
        ),
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Also sync to Google Drive after downloading",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        help="Only process specific symbols e.g. NSE:RELIANCE-EQ NSE:INFY-EQ",
    )
    args = parser.parse_args()

    setup_logging()
    logger.info(f"=== Nifty F&O Data Pipeline | {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    # ── Status / rebuild modes (no API needed) ───────────────
    if args.mode == "status":
        print_manifest_summary()
        return

    if args.mode == "rebuild":
        rebuild_manifest_from_disk()
        print_manifest_summary()
        return

    if args.mode == "sync":
        run_sync()
        return

    # ── Determine symbol list ────────────────────────────────
    symbols = args.symbols if args.symbols else ALL_SYMBOLS

    # ── Authenticate with Fyers ──────────────────────────────
    logger.info("Authenticating with Fyers API...")
    fyers_client = get_fyers_client()
    logger.info("Authentication successful")

    # ── Run download ─────────────────────────────────────────
    force_full = (args.mode == "full")
    changed = run_download(fyers_client, symbols, force_full=force_full)

    # ── Optionally sync to Drive ─────────────────────────────
    if args.sync or args.mode == "full":
        logger.info("Syncing to Google Drive...")
        run_sync(changed if not force_full else None)

    print_manifest_summary()
    logger.info("Pipeline complete.")


if __name__ == "__main__":
    main()