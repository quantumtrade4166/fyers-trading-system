import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
from datetime import date, datetime
from pathlib import Path

OPTIONS_MANIFEST_FILE = Path("data/options/options_manifest.json")


def load_manifest() -> dict:
    if OPTIONS_MANIFEST_FILE.exists():
        return json.loads(OPTIONS_MANIFEST_FILE.read_text())
    return {"last_updated": None, "contracts": {}}


def save_manifest(manifest: dict):
    OPTIONS_MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    manifest["last_updated"] = str(datetime.now())
    OPTIONS_MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))


def contract_key(expiry: date, strike: int, option_type: str) -> str:
    return f"{expiry}_{strike}_{option_type}"


def mark_fetched(manifest: dict, expiry: date, strike: int, option_type: str, result: dict):
    key = contract_key(expiry, strike, option_type)
    manifest["contracts"][key] = {
        "expiry":     str(expiry),
        "strike":     strike,
        "type":       option_type,
        "status":     result.get("status"),
        "bars":       result.get("bars", 0),
        "date_from":  result.get("date_from"),
        "date_to":    result.get("date_to"),
        "fetched_on": str(date.today()),
    }


def is_fetched(manifest: dict, expiry: date, strike: int, option_type: str) -> bool:
    key = contract_key(expiry, strike, option_type)
    return manifest["contracts"].get(key, {}).get("status") in ("success", "no_data", "up_to_date")


def print_summary(manifest: dict):
    contracts = manifest.get("contracts", {})
    success    = sum(1 for v in contracts.values() if v.get("status") == "success")
    no_data    = sum(1 for v in contracts.values() if v.get("status") == "no_data")
    total_bars = sum(v.get("bars", 0) for v in contracts.values())

    print(f"\n{'='*55}")
    print("  OPTIONS MANIFEST SUMMARY")
    print(f"{'='*55}")
    print(f"  Last updated      : {manifest.get('last_updated', 'Never')}")
    print(f"  Contracts fetched : {success}")
    print(f"  No data / expired : {no_data}")
    print(f"  Total 1-min bars  : {total_bars:,}")
    print(f"{'='*55}\n")
