"""
One-time conversion: Nifty_option_historical.zip (daily 1-min option chain CSVs)
-> year-partitioned Parquet under data/NSE_NIFTY_OPTIONS/{year}/ohlcv_1min.parquet

Reads directly out of the zip (never extracts raw CSVs to disk) to avoid
tripling disk usage. Source: "Nifty fno 2021-26/Nifty_option_historical.zip"
"""
import sys
import re
import zipfile
import io
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ZIP_PATH = Path("Nifty fno 2021-26/Nifty_option_historical.zip")
OUT_DIR = Path("data/NSE_NIFTY_OPTIONS")

DTYPES = {
    "strike_label": "category",
    "option_type": "category",
    "open": "float32",
    "high": "float32",
    "low": "float32",
    "close": "float32",
    "volume": "int64",
    "oi": "int64",
    "iv": "float32",
    "strike_price": "int32",
    "spot": "float32",
}

FNAME_RE = re.compile(r"NIFTY_(\d{4}-\d{2}-\d{2})_1m\.csv$")


def main():
    z = zipfile.ZipFile(ZIP_PATH)
    entries = []
    for n in z.namelist():
        if "__MACOSX" in n or not n.endswith(".csv"):
            continue
        m = FNAME_RE.search(n)
        if not m:
            continue
        entries.append((m.group(1), n))
    entries.sort()

    by_year = {}
    for date_str, name in entries:
        by_year.setdefault(date_str[:4], []).append((date_str, name))

    print(f"Found {len(entries)} daily files across {len(by_year)} years")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for year in sorted(by_year):
        files = by_year[year]
        frames = []
        for date_str, name in files:
            with z.open(name) as f:
                raw = f.read()
            df = pd.read_csv(io.BytesIO(raw), parse_dates=["datetime"])
            for col, dt in DTYPES.items():
                df[col] = df[col].astype(dt)
            frames.append(df)

        year_df = pd.concat(frames, ignore_index=True)
        year_df["date"] = year_df["datetime"].dt.date.astype(str)
        year_df.sort_values(["datetime", "strike_price", "option_type"], inplace=True)
        year_df.reset_index(drop=True, inplace=True)

        year_dir = OUT_DIR / year
        year_dir.mkdir(parents=True, exist_ok=True)
        out_path = year_dir / "ohlcv_1min.parquet"
        year_df.to_parquet(out_path, index=False)

        size_mb = out_path.stat().st_size / 1e6
        print(f"{year}: {len(files)} days, {len(year_df):,} rows -> {out_path} ({size_mb:.1f} MB)")

    z.close()
    print("Done.")


if __name__ == "__main__":
    main()
