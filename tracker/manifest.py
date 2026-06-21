# ============================================================
# tracker/manifest.py
# Maintains a JSON manifest of all downloaded data.
# This is what Claude reads at the start of each session
# to know exactly what data is available.
# ============================================================

import json
import logging
from pathlib import Path
from datetime import date, datetime
import sys
import pandas as pd

sys.path.append(str(Path(__file__).parent.parent))
from config.settings import DATA_DIR, TRACKER_DIR

logger = logging.getLogger(__name__)

MANIFEST_FILE = TRACKER_DIR / "data_manifest.json"


def load_manifest() -> dict:
    """Load the current manifest or return an empty one."""
    if MANIFEST_FILE.exists():
        return json.loads(MANIFEST_FILE.read_text())
    return {"last_updated": None, "symbols": {}}


def save_manifest(manifest: dict):
    """Save the manifest to disk."""
    manifest["last_updated"] = str(datetime.now())
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))


def update_manifest_for_symbol(symbol: str, result: dict):
    """Update the manifest entry for one symbol after a download."""
    manifest = load_manifest()
    manifest["symbols"][symbol] = {
        "status": result.get("status"),
        "bars_total": result.get("bars", 0),
        "date_from": result.get("date_from"),
        "date_to": result.get("date_to"),
        "last_checked": str(date.today()),
    }
    save_manifest(manifest)


def rebuild_manifest_from_disk():
    """
    Scan all local Parquet files and rebuild the manifest from scratch.
    Useful if the manifest gets out of sync.
    """
    logger.info("Rebuilding manifest from disk...")
    manifest = {"last_updated": None, "symbols": {}}

    for parquet_file in DATA_DIR.rglob("ohlcv_5min.parquet"):
        try:
            df = pd.read_parquet(parquet_file, columns=["datetime", "symbol"])
            if df.empty:
                continue
            symbol = df["symbol"].iloc[0]
            date_from = str(pd.to_datetime(df["datetime"].min()).date())
            date_to   = str(pd.to_datetime(df["datetime"].max()).date())

            if symbol not in manifest["symbols"]:
                manifest["symbols"][symbol] = {
                    "status": "success",
                    "bars_total": 0,
                    "date_from": date_from,
                    "date_to": date_to,
                    "last_checked": str(date.today()),
                }
            else:
                # Extend date range if multiple year files
                existing = manifest["symbols"][symbol]
                if date_from < existing["date_from"]:
                    existing["date_from"] = date_from
                if date_to > existing["date_to"]:
                    existing["date_to"] = date_to

            manifest["symbols"][symbol]["bars_total"] += len(df)

        except Exception as e:
            logger.warning(f"Could not read {parquet_file}: {e}")

    save_manifest(manifest)
    logger.info(f"Manifest rebuilt: {len(manifest['symbols'])} symbols")
    return manifest


def print_manifest_summary():
    """Print a human-readable summary of available data."""
    manifest = load_manifest()
    symbols = manifest.get("symbols", {})
    success = [s for s, v in symbols.items() if v.get("status") == "success"]
    failed  = [s for s, v in symbols.items() if v.get("status") not in ("success", "up_to_date")]

    print(f"\n{'='*55}")
    print(f"  DATA MANIFEST SUMMARY")
    print(f"{'='*55}")
    print(f"  Last updated : {manifest.get('last_updated', 'Never')}")
    print(f"  Total symbols: {len(symbols)}")
    print(f"  Downloaded   : {len(success)}")
    print(f"  Failed/missing: {len(failed)}")
    if success:
        sample = symbols[success[0]]
        print(f"  Date range   : {sample.get('date_from')} → {sample.get('date_to')}")
    print(f"{'='*55}\n")

    if failed:
        print("  Symbols with issues:")
        for s in failed[:10]:
            print(f"    - {s}: {symbols[s].get('status')}")
        if len(failed) > 10:
            print(f"    ... and {len(failed)-10} more")
        print()
