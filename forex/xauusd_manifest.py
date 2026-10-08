import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
from datetime import datetime
from pathlib import Path

MANIFEST_FILE = Path(__file__).parent / "xauusd_manifest.json"


def load_manifest() -> dict:
    if MANIFEST_FILE.exists():
        return json.loads(MANIFEST_FILE.read_text())
    return {"last_updated": None, "months": {}}


def save_manifest(manifest: dict):
    manifest["last_updated"] = str(datetime.now())
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def mark_fetched(manifest: dict, year: int, month: int, result: dict):
    manifest["months"][month_key(year, month)] = {
        "status":     result.get("status"),
        "rows":       result.get("rows", 0),
        "fetched_on": str(datetime.now().date()),
    }


def is_fetched(manifest: dict, year: int, month: int) -> bool:
    return manifest["months"].get(month_key(year, month), {}).get("status") in ("success", "no_data")


def print_summary(manifest: dict):
    months = manifest.get("months", {})
    success = sum(1 for v in months.values() if v.get("status") == "success")
    no_data = sum(1 for v in months.values() if v.get("status") == "no_data")
    failed  = sum(1 for v in months.values() if v.get("status") == "failed")
    total_rows = sum(v.get("rows", 0) for v in months.values())

    print(f"\n{'='*55}")
    print("  XAUUSD 1s MANIFEST SUMMARY")
    print(f"{'='*55}")
    print(f"  Last updated    : {manifest.get('last_updated', 'Never')}")
    print(f"  Months fetched  : {success}")
    print(f"  No data         : {no_data}")
    print(f"  Failed          : {failed}")
    print(f"  Total 1s bars   : {total_rows:,}")
    print(f"{'='*55}\n")
