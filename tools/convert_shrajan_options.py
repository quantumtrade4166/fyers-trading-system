"""Convert Shrajan's shared NIFTY/SENSEX option zips (one CSV per strike+expiry)
into year-partitioned parquet:

  data/SHRAJAN_OPTIONS/{NIFTY|SENSEX}/{year}/ohlcv_1min.parquet
  columns: datetime, expiry, strike, right (CE/PE), open, high, low, close,
           volume, oi, src  (src = short zip name; duplicates across zips -> first kept)

Step 1 writes one part file per (zip, index, year); step 2 merges + dedupes per year.
"""
import sys, re, zipfile, io, gc
from pathlib import Path
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"C:\trading\fyers_data_pipeline\data\SHRAJAN_OPTIONS")
ZIPS = ROOT / "zips"
PARTS = ROOT / "_parts"
PARTS.mkdir(exist_ok=True)
PAT = re.compile(r"(call|put)_strike_(\d+)_expiry_(\d{7,8})\.csv$")
ORDER = ["Split_nifty_TrueData&GDFL", "NIFTY 2025 NEW DATA", "Final_Splitting", "Final Splitting",
         "Aug 2023 - Dec 2024 - SENSEX", "Sesex Options 2025", "Sensex_Tick_1Min"]


def index_of(name):
    return "SENSEX" if re.search(r"sensex|sesex", name, re.I) else "NIFTY"


def step1():
    for rank, z in enumerate(sorted(ZIPS.glob("*.zip"), key=lambda p: [i for i, o in enumerate(ORDER) if p.name.startswith(o)][0])):
        idx, src = index_of(z.name), f"{rank}:{z.name.replace(' - Copy.zip', '')[:40]}"
        if list(PARTS.glob(f"z{rank}_*.parquet")):
            print("skip (done)", z.name, flush=True)
            continue
        buf = []
        with zipfile.ZipFile(z) as zf:
            for i in zf.infolist():
                m = PAT.search(i.filename)
                if not m:
                    continue
                df = pd.read_csv(io.BytesIO(zf.read(i)), dtype={"datetime": str})
                if df.empty:
                    continue
                df["datetime"] = pd.to_datetime(df["datetime"].str[:12], format="%Y%m%d%H%M")
                df["expiry"] = pd.to_datetime(m.group(3).zfill(8), format="%d%m%Y")
                df["strike"] = int(m.group(2))
                df["right"] = "CE" if m.group(1) == "call" else "PE"
                df = df.rename(columns={"openinterest": "oi"})
                buf.append(df[["datetime", "expiry", "strike", "right", "open", "high", "low", "close", "volume", "oi"]])
        allz = pd.concat(buf, ignore_index=True)
        del buf
        allz["volume"] = pd.to_numeric(allz["volume"], errors="coerce").fillna(0).astype("int64")
        allz["oi"] = pd.to_numeric(allz["oi"], errors="coerce").fillna(0).astype("int64")
        for c in ("open", "high", "low", "close"):
            allz[c] = allz[c].astype("float32")
        allz["src"] = src
        for y, g in allz.groupby(allz["datetime"].dt.year):
            g.to_parquet(PARTS / f"z{rank}_{idx}_{y}.parquet", index=False)
        print(f"{z.name}: {len(allz):,} rows -> {idx}", flush=True)
        del allz
        gc.collect()


def step2():
    keys = sorted({(p.stem.split("_")[1], p.stem.split("_")[2]) for p in PARTS.glob("z*_*.parquet")})
    for idx, y in keys:
        out = ROOT / idx / y / "ohlcv_1min.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        parts = sorted(PARTS.glob(f"z*_{idx}_{y}.parquet"))
        df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
        n0 = len(df)
        df = df.sort_values(["src"], kind="stable").drop_duplicates(["datetime", "expiry", "strike", "right"], keep="first")
        df = df.sort_values(["datetime", "expiry", "strike", "right"]).reset_index(drop=True)
        tmp = out.with_suffix(".tmp")
        df.to_parquet(tmp, index=False, compression="snappy")
        tmp.replace(out)
        print(f"{idx} {y}: {len(df):,} rows ({n0 - len(df):,} duplicates dropped), "
              f"{df.datetime.min()} -> {df.datetime.max()}, {df.expiry.nunique()} expiries", flush=True)
        del df
        gc.collect()


if __name__ == "__main__":
    step1()
    step2()
    print("DONE", flush=True)
