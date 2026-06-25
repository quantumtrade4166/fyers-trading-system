"""
core/premium_builder.py
=======================

Canonical combined-premium + VWAP engine for the strangle strategy.

VWAP method validated against iCharts on 2026-06-25 (see memory note
`reference_combined_vwap_formula`):
  - combined candle built from 1-MINUTE legs; high/low from SYNCHRONIZED
    open & close points (CE+PE sampled at the same instant) — NOT from summing
    each leg's independent high/low (that inflates the wicks).
  - VWAP = Σ(typical × volume) / Σ(volume), typical = (high+low+close)/3,
    cumulative from 09:15, resets each day.

Open & close are exact vs iCharts; high/low are the best obtainable from 1-min
history (mean ~1.6 error on violent candles). Live tick-built candles match
iCharts exactly — this module is shared so historical and live use identical math.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd

MKT_START = pd.Timestamp("09:15").time()
MKT_END   = pd.Timestamp("15:30").time()


def fetch_legs(client, symbol: str, from_date: str, to_date: str,
               resolution: str = "1") -> pd.DataFrame:
    """Fetch 1-minute OHLCV for a single option leg from the Fyers history API.
    Returns columns: datetime, open, high, low, close, volume."""
    data = {
        "symbol":      symbol,
        "resolution":  resolution,
        "date_format": "1",
        "range_from":  from_date,
        "range_to":    to_date,
        "cont_flag":   "1",
    }
    resp = client.history(data=data)
    if resp.get("s") != "ok":
        raise RuntimeError(f"Fyers history failed for {symbol}: {resp}")
    candles = resp.get("candles", [])
    if not candles:
        raise RuntimeError(f"No candles for {symbol} ({from_date}→{to_date})")

    cols = ["epoch", "open", "high", "low", "close", "volume"]
    if len(candles[0]) == 7:
        cols.append("oi")
    df = pd.DataFrame(candles, columns=cols)
    df["datetime"] = (
        pd.to_datetime(df["epoch"], unit="s")
        .dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    )
    df = df.drop(columns=["epoch"])
    df = df[(df["datetime"].dt.time >= MKT_START) & (df["datetime"].dt.time <= MKT_END)]
    df = df.drop_duplicates(subset="datetime", keep="first")
    return df[["datetime", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def build_combined(ce: pd.DataFrame, pe: pd.DataFrame) -> pd.DataFrame:
    """Merge 1-min CE & PE, roll up to 5-min combined-premium candles with
    smoothed wicks, and attach cumulative typical-price VWAP + entry/exit signals.

    Returns one row per 5-min candle with:
      datetime, date, open, high, low, close, volume, typ, vwap,
      is_red, below_vwap, above_vwap, entry_signal, exit_signal
    """
    m = ce.merge(pe, on="datetime", suffixes=("_ce", "_pe"))
    if m.empty:
        raise RuntimeError("CE/PE have no overlapping timestamps")
    m["c_open"]  = m["open_ce"]  + m["open_pe"]
    m["c_close"] = m["close_ce"] + m["close_pe"]
    m["c_vol"]   = m["volume_ce"] + m["volume_pe"]
    m = m.set_index("datetime")

    def agg(x: pd.DataFrame) -> pd.Series:
        pts = pd.concat([x["c_open"], x["c_close"]])      # synchronized price points
        return pd.Series({
            "open":   x["c_open"].iloc[0],
            "high":   pts.max(),
            "low":    pts.min(),
            "close":  x["c_close"].iloc[-1],
            "volume": x["c_vol"].sum(),
        })

    bars = (
        m.resample("5min", label="left", closed="left", origin="start_day")
         .apply(agg).dropna()
    )
    bars = bars[(bars.index.time >= MKT_START) & (bars.index.time <= MKT_END)]
    out = bars.reset_index()
    out["date"] = out["datetime"].dt.date

    out["typ"] = (out["high"] + out["low"] + out["close"]) / 3
    out["pv"]  = out["typ"] * out["volume"]
    grp = out.groupby("date")
    out["vwap"] = grp["pv"].cumsum() / grp["volume"].cumsum()
    out = out.drop(columns=["pv"])

    out["is_red"]       = out["close"] < out["open"]
    out["below_vwap"]   = out["close"] < out["vwap"]
    out["above_vwap"]   = out["close"] > out["vwap"]
    out["entry_signal"] = out["below_vwap"] & out["is_red"]   # sell signal
    out["exit_signal"]  = out["above_vwap"]                   # buy-back signal
    return out


def combined_for_strikes(client, ce_sym: str, pe_sym: str,
                         from_date: str, to_date: str) -> pd.DataFrame:
    """Convenience: fetch both legs (1-min) and return the combined+VWAP frame."""
    ce = fetch_legs(client, ce_sym, from_date, to_date)
    pe = fetch_legs(client, pe_sym, from_date, to_date)
    return build_combined(ce, pe)
