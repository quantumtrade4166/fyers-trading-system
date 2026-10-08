# ============================================================
# etf/etf_universe.py
# NSE ETF universe for the ETF strategy work.
#
# ETFs are mutual-fund units, so their ISIN starts with "INF"
# (ordinary shares start with "INE"). That single rule pulls the
# whole exchange-traded ETF list out of the Fyers symbol master.
# ============================================================

import csv
import io
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

SYM_MASTER_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"

# Column positions in the Fyers NSE_CM master (no header row)
COL_DESC = 1
COL_ISIN = 5
COL_TICKER = 9

# ── Core list ────────────────────────────────────────────────
# The liquid, backtest-worthy ETFs: one or two per exposure rather
# than all 20 AMC clones of the same index. Everything else in the
# 340-symbol universe is picked up by --all.
CORE_ETFS = [
    # Broad index
    "NSE:NIFTYBEES-EQ",     # Nifty 50            (deepest history: 2002)
    "NSE:SETFNIF50-EQ",     # Nifty 50   (SBI)
    "NSE:NIFTYIETF-EQ",     # Nifty 50   (ICICI)
    "NSE:MOM50-EQ",         # Nifty 50   (Motilal)
    "NSE:EQUAL50ADD-EQ",    # Nifty 50 Equal Weight
    "NSE:JUNIORBEES-EQ",    # Nifty Next 50
    # Banks / financials
    "NSE:BANKBEES-EQ",      # Bank Nifty
    "NSE:SETFNIFBK-EQ",     # Bank Nifty (SBI)
    "NSE:BANKIETF-EQ",      # Bank Nifty (ICICI)
    "NSE:PSUBNKBEES-EQ",    # PSU Bank
    "NSE:PVTBANIETF-EQ",    # Private Bank
    # Mid / small cap
    "NSE:MID150BEES-EQ",    # Nifty Midcap 150
    "NSE:MIDCAPETF-EQ",     # Nifty Midcap 150 (Mirae)
    "NSE:HDFCMID150-EQ",    # Nifty Midcap 150 (HDFC)
    "NSE:MOM100-EQ",        # Nifty Midcap 100
    "NSE:HDFCSML250-EQ",    # Nifty Smallcap 250
    "NSE:MOSMALL250-EQ",    # Nifty Smallcap 250 (Motilal)
    # Commodity
    "NSE:GOLDBEES-EQ",      # Gold
    "NSE:SETFGOLD-EQ",      # Gold (SBI)
    "NSE:GOLDIETF-EQ",      # Gold (ICICI)
    "NSE:SILVERBEES-EQ",    # Silver  (listed Jan 2022 — no long history exists)
    "NSE:SILVERIETF-EQ",    # Silver (ICICI)
    # International
    "NSE:MON100-EQ",        # Nasdaq 100
    "NSE:MAFANG-EQ",        # NYSE FANG+
    "NSE:HNGSNGBEES-EQ",    # Hang Seng
    # Factor / smart beta
    "NSE:ALPHA-EQ",         # Nifty Alpha 50
    "NSE:NV20-EQ",          # Nifty 50 Value 20
    "NSE:MOMOMENTUM-EQ",    # Nifty 200 Momentum 30
    "NSE:MOM30IETF-EQ",     # Nifty 200 Momentum 30 (ICICI)
    "NSE:LOWVOL1-EQ",       # Nifty 100 Low Vol 30
    "NSE:DIVOPPBEES-EQ",    # Dividend Opportunities
    # Sector
    "NSE:ITBEES-EQ",
    "NSE:PHARMABEES-EQ",
    "NSE:AUTOBEES-EQ",
    "NSE:CONSUMBEES-EQ",
    "NSE:INFRABEES-EQ",
    "NSE:MOREALTY-EQ",
    "NSE:MOHEALTH-EQ",
    "NSE:MODEFENCE-EQ",
    # Theme / other
    "NSE:CPSEETF-EQ",
    "NSE:ICICIB22-EQ",      # Bharat 22
    # Cash / debt parking (useful as the "risk off" leg in rotation)
    "NSE:LIQUIDBEES-EQ",
    "NSE:GILT5YBEES-EQ",
]


def fetch_all_etfs(cache: Path | None = None) -> list[tuple[str, str]]:
    """
    Download the Fyers NSE symbol master and return every exchange-traded
    ETF as (ticker, description), sorted by ticker.

    Excludes -MF / -SF instruments: those are mutual-fund plans that also
    carry INF ISINs but are not traded on the order book.
    """
    if cache and cache.exists():
        raw = cache.read_text(encoding="utf-8", errors="replace")
    else:
        raw = urllib.request.urlopen(SYM_MASTER_URL, timeout=90).read().decode("utf-8", "replace")
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(raw, encoding="utf-8")

    etfs = []
    for row in csv.reader(io.StringIO(raw)):
        if len(row) <= COL_TICKER:
            continue
        ticker, isin = row[COL_TICKER], row[COL_ISIN]
        if not ticker.startswith("NSE:") or not ticker.endswith("-EQ"):
            continue
        if not isin.startswith("INF"):
            continue
        etfs.append((ticker, row[COL_DESC].strip()))

    return sorted(set(etfs))


def short_name(ticker: str) -> str:
    """NSE:NIFTYBEES-EQ -> NIFTYBEES (used as the parquet filename)."""
    return ticker.replace("NSE:", "").replace("-EQ", "")


if __name__ == "__main__":
    all_etfs = fetch_all_etfs()
    print(f"Exchange-traded NSE ETFs: {len(all_etfs)}")
    print(f"Core list: {len(CORE_ETFS)}")
    missing = [s for s in CORE_ETFS if s not in {t for t, _ in all_etfs}]
    print(f"Core symbols not found in master: {missing or 'none'}")
