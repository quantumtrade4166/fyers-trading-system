"""Drop manifest entries whose rows are not actually on disk.

Guards the resume against silent gaps: if a run was killed between recording a
contract-day as done and flushing its rows, the manifest would claim data the
parquet does not have, and the resume would skip it forever.

Newer runs commit the manifest only after a successful parquet write, so this is
mainly a repair for manifests written before that change — and a cheap sanity
check any time a run ends unexpectedly.

    python -m options.breeze.reconcile            # report only
    python -m options.breeze.reconcile --fix      # drop the phantom entries
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import json

import pandas as pd

from options.breeze.config import DATA_DIR, MANIFEST_PATH


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile manifest against parquet")
    ap.add_argument("--fix", action="store_true", help="remove unbacked entries")
    args = ap.parse_args()

    if not MANIFEST_PATH.exists():
        print("No manifest — nothing to reconcile.")
        return 0

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    print(f"Manifest entries: {len(manifest):,}")

    # Build the set of (stock, interval, date, expiry, strike, type) actually stored.
    on_disk: set = set()
    for path in sorted(DATA_DIR.rglob("*.parquet")):
        stock = path.parent.parent.name
        interval = path.stem.replace("ohlcv_", "")
        df = pd.read_parquet(path, columns=["date", "expiry", "strike_price",
                                            "option_type"])
        for row in df.drop_duplicates().itertuples():
            right = "call" if str(row.option_type).upper().startswith("C") else "put"
            on_disk.add((stock, interval, str(row.date),
                         str(pd.Timestamp(row.expiry).date()),
                         int(row.strike_price), right))
        print(f"  {path.relative_to(DATA_DIR)}: {len(df):,} contract-rows")

    print(f"Distinct contract-days on disk: {len(on_disk):,}")

    phantom = []
    for key, entry in manifest.items():
        if entry.get("status") != "ok":
            continue          # 'empty' legitimately has no rows
        parts = key.split("|")
        if len(parts) < 7:
            continue
        _exch, stock, trade_date, expiry, strike, right, interval = parts[:7]
        if (stock, interval, trade_date, expiry, int(strike), right) not in on_disk:
            phantom.append(key)

    print(f"\nEntries marked 'ok' with NO rows on disk: {len(phantom):,}")
    for key in phantom[:10]:
        print(f"  {key}")
    if len(phantom) > 10:
        print(f"  ... and {len(phantom) - 10:,} more")

    if phantom and args.fix:
        for key in phantom:
            del manifest[key]
        MANIFEST_PATH.write_text(json.dumps(manifest), encoding="utf-8")
        print(f"\nRemoved {len(phantom):,} entries — they will be re-downloaded.")
    elif phantom:
        print("\nRun with --fix to drop them so the resume re-fetches them.")
    else:
        print("\nManifest and parquet agree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
