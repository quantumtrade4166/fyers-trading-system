"""
core/strike_selector.py
=======================

Select the strangle strikes per the strategy rule:
  - at 9:20 (when the 9:15 candle closes), find ATM from spot
  - scan OTM levels outward: OTM1, OTM2, ...  (SAME level for CE and PE)
  - pick the FIRST level where CE_LTP + PE_LTP <= premium threshold
  - same strikes are fixed for the whole day

Two entry points:
  select_strangle_live(...)        -> uses Fyers optionchain (real-time, Phase 1)
  select_strangle_historical(...)  -> reconstructs the 9:15-close premium from
                                      history (used by the EOD chart archive)
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import datetime as dt
from pathlib import Path

import pandas as pd

from core import symbol_master
from core.premium_builder import fetch_legs

_PARAMS = json.loads((Path(__file__).resolve().parents[1] / "config" / "parameters.json").read_text())
INDEX_SYMBOL   = _PARAMS["index_symbols"]
STRIKE_INTERVAL = _PARAMS["strike_interval"]
MAX_OTM_SCAN = 40


def threshold_for(index: str, d_dte: int) -> float:
    key = f"{d_dte}_DTE"
    return _PARAMS[key][f"{index.lower()}_premium_threshold"]


def atm_strike(spot: float, index: str) -> int:
    iv = STRIKE_INTERVAL[index]
    return int(round(spot / iv) * iv)


def _price_at_920(df: pd.DataFrame) -> float | None:
    """Close of the 9:15 FIVE-minute candle = the price at 9:20 that triggers the
    strike selection. From 1-min data this is the last bar before 09:20 (the 09:19
    bar). Using the 09:15 one-minute bar instead reads the ~09:16 open and shifts
    the ATM by a strike on a volatile open — that was the original bug."""
    pre920 = df[df["datetime"].dt.time < pd.Timestamp("09:20").time()]
    return None if pre920.empty else float(pre920.iloc[-1]["close"])


def spot_at_open(client, index: str, date_str: str) -> float:
    """Spot at the 9:15 candle close (the value the 9:20 selection sees)."""
    idx = fetch_legs(client, INDEX_SYMBOL[index], date_str, date_str)
    px = _price_at_920(idx)
    if px is None:
        raise RuntimeError(f"No pre-09:20 index bar for {index} {date_str}")
    return px


def _leg_915_close(client, symbol: str, date_str: str) -> float | None:
    try:
        df = fetch_legs(client, symbol, date_str, date_str)
    except RuntimeError:
        return None
    return _price_at_920(df)


def select_strangle_historical(client, index: str, expiry: dt.date,
                               threshold: float, date_str: str) -> dict:
    """Reconstruct the 9:20 strike selection from history (for EOD archiving)."""
    spot = spot_at_open(client, index, date_str)
    atm = atm_strike(spot, index)
    iv = STRIKE_INTERVAL[index]

    for n in range(1, MAX_OTM_SCAN + 1):
        ce_strike, pe_strike = atm + n * iv, atm - n * iv
        ce_sym = symbol_master.find_symbol(index, expiry, ce_strike, "CE")
        pe_sym = symbol_master.find_symbol(index, expiry, pe_strike, "PE")
        if not ce_sym or not pe_sym:
            continue
        ce_ltp = _leg_915_close(client, ce_sym, date_str)
        pe_ltp = _leg_915_close(client, pe_sym, date_str)
        if ce_ltp is None or pe_ltp is None:
            continue
        combined = ce_ltp + pe_ltp
        if combined <= threshold:
            return {
                "index": index, "expiry": expiry.isoformat(), "otm_level": n,
                "ce_symbol": ce_sym, "pe_symbol": pe_sym,
                "ce_strike": ce_strike, "pe_strike": pe_strike,
                "spot": spot, "atm": atm,
                "combined_premium": round(combined, 2), "threshold": threshold,
            }
    raise RuntimeError(f"No strangle <= {threshold} within {MAX_OTM_SCAN} OTM levels "
                       f"for {index} {date_str}")


def select_strangle_live(client, index: str, expiry: dt.date, threshold: float) -> dict:
    """Real-time selection via Fyers optionchain (Phase 1 live use)."""
    resp = client.optionchain(data={"symbol": INDEX_SYMBOL[index], "strikecount": MAX_OTM_SCAN})
    if resp.get("s") != "ok":
        raise RuntimeError(f"optionchain failed for {index}: {resp}")
    rows = resp["data"]["optionsChain"]
    chain = pd.DataFrame(rows)
    chain = chain[chain["option_type"].isin(["CE", "PE"])]
    spot = float(resp["data"].get("indiavixData", {}).get("ltp", 0)) or float(chain["ltp"].iloc[0])
    # build {strike: {CE: ltp, PE: ltp}} restricted to our expiry where available
    atm = atm_strike(float(resp["data"]["last_price"]), index) if "last_price" in resp["data"] else None
    iv = STRIKE_INTERVAL[index]
    ltp = {(int(r["strike_price"]), r["option_type"]): float(r["ltp"]) for _, r in chain.iterrows()}
    if atm is None:
        atm = atm_strike(float(resp["data"]["last_price"]), index)
    for n in range(1, MAX_OTM_SCAN + 1):
        ce_strike, pe_strike = atm + n * iv, atm - n * iv
        ce_ltp = ltp.get((ce_strike, "CE")); pe_ltp = ltp.get((pe_strike, "PE"))
        if ce_ltp is None or pe_ltp is None:
            continue
        if ce_ltp + pe_ltp <= threshold:
            return {
                "index": index, "expiry": expiry.isoformat(), "otm_level": n,
                "ce_symbol": symbol_master.find_symbol(index, expiry, ce_strike, "CE"),
                "pe_symbol": symbol_master.find_symbol(index, expiry, pe_strike, "PE"),
                "ce_strike": ce_strike, "pe_strike": pe_strike, "atm": atm,
                "combined_premium": round(ce_ltp + pe_ltp, 2), "threshold": threshold,
            }
    raise RuntimeError(f"No live strangle <= {threshold} for {index}")
