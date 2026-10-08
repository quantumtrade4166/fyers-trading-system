"""
fetch_corporate_actions.py
Download NSE's corporate-actions register, 2005 -> today, and turn split/bonus
records into price adjustment factors.

Needed because NSE bhavcopy PREVCLOSE is NOT corporate-action adjusted -- it is
simply the previous day's raw close. Verified on TCS 2018-05-31 (1:1 bonus):
close 1741.05, prev_close 3514.10. So splits must come from this register.

NSE ratio conventions
  "Bonus a:b"                -> a new shares for every b held
                                price multiplier = b / (a + b)
  "Fv Split Rs.X To Re.Y"    -> price multiplier = Y / X

Output: Bhavcopy/_matrix/corporate_actions.csv
        symbol, ex_date, kind, ratio_text, factor
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import re
import time
from pathlib import Path

import pandas as pd
import requests

OUT = Path(r"G:\fyers_data_pipeline\Bhavcopy\_matrix")
API = "https://www.nseindia.com/api/corporates-corporateActions"

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
})


def warm():
    try:
        S.get("https://www.nseindia.com/companies-listing/corporate-filings-actions", timeout=20)
    except Exception:
        pass


def parse_factor(subject: str):
    """Return (kind, factor) where factor multiplies PRE-event prices, else None.

    Handles COMPOUND events, e.g. "Bonus 1:1 And Face Value Split From Rs.10 To Re.1"
    -> 0.5 * 0.1 = 0.05. Taking only the first match understates these badly.
    """
    s = " ".join(str(subject).split())

    # A bonus issue of DEBENTURES / preference shares / NCRPS does not change the
    # equity share count, so it must NOT adjust the price. NTPC 2015 "Bonus
    # Debentures 1:1" and TVSMOTOR 2025 "Bonus Ncrps 4:1" were being adjusted as
    # if they were equity bonuses.
    if re.search(r"bonus\s+(?:debenture|ncrps|ncd|preference|pref)", s, re.I):
        return None

    factor, kinds = 1.0, []

    # NSE wording varies: "Fv Split Rs.10 To Re.1" and
    # "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share"
    m = re.search(r"split.*?(?:rs\.?|re\.?)\s*([\d.]+)\s*(?:/-)?[^\d]{0,30}?"
                  r"to\s*(?:rs\.?|re\.?)\s*([\d.]+)", s, re.I)
    if m:
        old_fv, new_fv = float(m.group(1)), float(m.group(2))
        if old_fv > 0 and new_fv > 0 and new_fv < old_fv:
            factor *= new_fv / old_fv
            kinds.append("split")

    m = re.search(r"bonus\D{0,12}(\d+)\s*:\s*(\d+)", s, re.I)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0:
            factor *= b / (a + b)
            kinds.append("bonus")

    if not kinds:
        return None
    return ("+".join(kinds), factor)


def main():
    warm()
    rows, windows = [], []
    d = pd.Timestamp("2005-01-01")
    end = pd.Timestamp.today().normalize()
    while d < end:
        nxt = min(d + pd.DateOffset(months=2), end)
        windows.append((d, nxt))
        d = nxt + pd.Timedelta(days=1)

    print(f"fetching {len(windows)} two-month windows 2005 -> today")
    fails = 0
    for n, (a, b) in enumerate(windows, 1):
        url = (f"{API}?index=equities&from_date={a.strftime('%d-%m-%Y')}"
               f"&to_date={b.strftime('%d-%m-%Y')}")
        got = None
        for attempt in range(3):
            try:
                r = S.get(url, timeout=30)
                if r.status_code == 200:
                    got = r.json()
                    break
                time.sleep(1.5 * (attempt + 1))
                warm()
            except Exception:
                time.sleep(1.5 * (attempt + 1))
                warm()
        if got is None:
            fails += 1
        else:
            for x in got:
                rows.append({"symbol": str(x.get("symbol", "")).strip(),
                             "ex_date": x.get("exDate"),
                             "subject": str(x.get("subject", "")).strip()})
        if n % 20 == 0:
            print(f"  {n}/{len(windows)}  rows={len(rows)}  fails={fails}", flush=True)
        time.sleep(0.5)

    df = pd.DataFrame(rows).drop_duplicates()
    df["ex_date"] = pd.to_datetime(df["ex_date"], format="%d-%b-%Y", errors="coerce")
    df = df.dropna(subset=["ex_date"])

    parsed = df["subject"].apply(parse_factor)
    df["kind"] = [p[0] if p else None for p in parsed]
    df["factor"] = [p[1] if p else None for p in parsed]
    ca = df.dropna(subset=["factor"]).sort_values(["symbol", "ex_date"])
    ca = ca.drop_duplicates(subset=["symbol", "ex_date", "factor"])

    OUT.mkdir(parents=True, exist_ok=True)
    ca.to_csv(OUT / "corporate_actions.csv", index=False)
    df.to_csv(OUT / "corporate_actions_raw.csv", index=False)

    print(f"\ntotal records {len(df):,} | split/bonus parsed {len(ca):,} "
          f"across {ca['symbol'].nunique():,} symbols | window fails {fails}")
    print(ca["kind"].value_counts().to_string())
    print("\nby year:")
    print(ca.groupby(ca["ex_date"].dt.year).size().to_string())


if __name__ == "__main__":
    main()
