"""
download_bhavcopy.py
Download the full NSE daily bhavcopy archive, 2005 -> today.

Why: our Nifty 500 universe is TODAY's constituent list, so the backtest never
buys the stocks that later died (DHFL, VAKRANGEE, PCJEWELLER, INFIBEAM, RCOM,
GITANJALI...). Several of those were top-40 momentum names immediately before
they collapsed. The bhavcopy contains EVERY stock that traded on each day,
including ones later delisted, so a point-in-time universe can be rebuilt from it.

Bonus: NSE adjusts PREVCLOSE for corporate actions, so wherever
CLOSE(t-1) != PREVCLOSE(t) there was a split/bonus and the ratio IS the
adjustment factor. The archive documents its own corporate actions.

Output: Bhavcopy/{YYYY}/{YYYY-MM-DD}.parquet
  columns: symbol, series, open, high, low, close, prev_close, volume, value

Resume-safe: already-downloaded days are skipped.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import io
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

ROOT    = Path(r"G:\fyers_data_pipeline")
OUT_DIR = ROOT / "Bhavcopy"
INDEX   = ROOT / "Nifty 500 Daily Fyers" / "_NIFTY50_INDEX.parquet"

MON   = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
SLEEP = 0.35          # politeness; NSE blocks aggressive clients
RETRY = 3

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
})


def warm():
    try:
        S.get("https://www.nseindia.com", timeout=20)
    except Exception:
        pass


def candidates(d: pd.Timestamp):
    dd, mm, yyyy = d.strftime("%d"), MON[d.month - 1], d.strftime("%Y")
    old = (f"https://nsearchives.nseindia.com/content/historical/EQUITIES/"
           f"{yyyy}/{mm}/cm{dd}{mm}{yyyy}bhav.csv.zip")
    sec = (f"https://nsearchives.nseindia.com/products/content/"
           f"sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv")
    new = (f"https://nsearchives.nseindia.com/content/cm/"
           f"BhavCopy_NSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip")
    if d.year >= 2024:
        return [("new", new), ("sec", sec), ("old", old)]
    if d.year >= 2020:
        return [("sec", sec), ("old", old), ("new", new)]
    return [("old", old), ("sec", sec), ("new", new)]


# each source -> {canonical: source column}
MAPS = {
    "old": {"symbol": "SYMBOL", "series": "SERIES", "open": "OPEN", "high": "HIGH",
            "low": "LOW", "close": "CLOSE", "prev_close": "PREVCLOSE",
            "volume": "TOTTRDQTY", "value": "TOTTRDVAL"},
    "sec": {"symbol": "SYMBOL", "series": "SERIES", "open": "OPEN_PRICE",
            "high": "HIGH_PRICE", "low": "LOW_PRICE", "close": "CLOSE_PRICE",
            "prev_close": "PREV_CLOSE", "volume": "TTL_TRD_QNTY",
            "value": "TURNOVER_LACS"},
    "new": {"symbol": "TckrSymb", "series": "SctySrs", "open": "OpnPric",
            "high": "HghPric", "low": "LwPric", "close": "ClsPric",
            "prev_close": "PrvsClsgPric", "volume": "TtlTradgVol",
            "value": "TtlTrfVal"},
}


def normalise(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    df.columns = [str(c).strip() for c in df.columns]
    m = MAPS[kind]
    missing = [v for v in m.values() if v not in df.columns]
    if missing:
        raise ValueError(f"{kind}: missing {missing}")
    out = df[[m[k] for k in m]].copy()
    out.columns = list(m.keys())
    out["symbol"] = out["symbol"].astype(str).str.strip()
    out["series"] = out["series"].astype(str).str.strip()
    out = out[out["series"] == "EQ"]
    for c in ("open", "high", "low", "close", "prev_close", "volume", "value"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    if kind == "sec":
        out["value"] = out["value"] * 100_000        # lakhs -> rupees
    return out.dropna(subset=["close"]).reset_index(drop=True)


def fetch_day(d: pd.Timestamp):
    for kind, url in candidates(d):
        for attempt in range(RETRY):
            try:
                r = S.get(url, timeout=40)
            except Exception:
                time.sleep(1.0 * (attempt + 1))
                continue
            if r.status_code == 404:
                break                                 # wrong format for this era
            if r.status_code in (401, 403, 429) or len(r.content) < 500:
                time.sleep(2.0 * (attempt + 1))
                warm()
                continue
            try:
                if url.endswith(".zip"):
                    z = zipfile.ZipFile(io.BytesIO(r.content))
                    raw = pd.read_csv(z.open(z.namelist()[0]))
                else:
                    raw = pd.read_csv(io.BytesIO(r.content))
                return normalise(raw, kind), kind
            except Exception:
                break
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    idx = pd.read_parquet(INDEX, columns=["close"])
    days = idx.loc[args.start:args.end].index if args.end else idx.loc[args.start:].index
    OUT_DIR.mkdir(exist_ok=True)

    todo = []
    for d in days:
        p = OUT_DIR / str(d.year) / f"{d.date()}.parquet"
        if not p.exists():
            todo.append(d)

    print(f"NSE bhavcopy: {len(todo)} of {len(days)} days to fetch -> {OUT_DIR}")
    warm()
    ok = fail = 0
    fails = []
    t0 = time.time()

    for n, d in enumerate(todo, 1):
        df, kind = fetch_day(d)
        if df is None or len(df) == 0:
            fail += 1
            fails.append(str(d.date()))
        else:
            sub = OUT_DIR / str(d.year)
            sub.mkdir(exist_ok=True)
            df.to_parquet(sub / f"{d.date()}.parquet")
            ok += 1
        time.sleep(SLEEP)

        if n % 100 == 0:
            el = time.time() - t0
            rate = n / el
            print(f"  {n}/{len(todo)}  ok={ok} fail={fail}  "
                  f"{el/60:.1f} min  ETA {(len(todo)-n)/rate/60:.0f} min", flush=True)

    print(f"\nDone in {(time.time()-t0)/60:.1f} min | ok={ok} fail={fail}")
    if fails:
        print(f"failed days ({len(fails)}): {fails[:25]}{' ...' if len(fails) > 25 else ''}")


if __name__ == "__main__":
    main()
