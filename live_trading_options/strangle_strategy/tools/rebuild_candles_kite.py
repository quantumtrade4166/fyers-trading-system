"""
tools/rebuild_candles_kite.py
=============================
Rebuild the combined-premium (VWAP) candle archives for a trading day from KITE
1-minute HISTORY, for days the live engine could not run on the Fyers feed
(e.g. the Fyers 5,000-req/day quota was exhausted before 09:20).

It reproduces EXACTLY what the live engine's `write_archive()` writes:
  data/chart_archive/{date}_{index}_V2.json   (tick-exact archive the engine seeds from)
  data/chart_archive/{date}_{index}.json      (V1 base layer)
same record shape {date,index,version,...,candles:[{time,o,h,l,c,volume,vwap}],events,pnl,selection}
so the dashboard chart and a mid-day engine reseed both work off it unchanged.

Only the DATA SOURCE changes: legs come from Kite `historical_data(..., "minute")`
(reusing live/kite_executor's client + instrument resolution) instead of Fyers.
Strike selection mirrors core.strike_selector (ATM from 09:15 spot, scan OTM levels,
first pair whose 09:15 combined premium <= the dte threshold).

Usage (on the VPS, with today's Kite token):
  .venv\\Scripts\\python.exe live_trading_options\\strangle_strategy\\tools\\rebuild_candles_kite.py \\
      --date 2026-10-21 --indices NIFTY SENSEX
No orders are ever placed; this is read-only against Kite history + local file writes.
"""

import sys
import json
import time
import argparse
import datetime as dt
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]          # .../strangle_strategy
sys.path.insert(0, str(ROOT))

from live import kite_executor as kx                 # get_kite, resolve, _instruments
from core.premium_builder import build_combined      # broker-agnostic combined+VWAP
from core.signal_engine import simulate_day, pair_trades, mtm_series
from core import strike_selector as ss               # atm_strike, STRIKE_INTERVAL, threshold_for, MAX_OTM_SCAN
from core import dte_calculator as dc                 # nearest_expiry_and_dte
from data.chart_archive import ARCHIVE_DIR

MKT_OPEN = dt.time(9, 15)
MKT_CLOSE = dt.time(15, 30)
_PACE = 0.34                                           # ~3 Kite history req/s cap


def _pace():
    time.sleep(_PACE)


def _index_spot_token(kite, index: str) -> int:
    """Kite instrument_token for the index SPOT (NIFTY 50 on NSE, SENSEX on BSE)."""
    exch = "NSE" if index == "NIFTY" else "BSE"
    want = "NIFTY 50" if index == "NIFTY" else "SENSEX"
    for r in kx._instruments(kite, exch):
        if r.get("tradingsymbol") == want and str(r.get("segment", "")).upper().endswith("INDICES"):
            return int(r["instrument_token"])
    # fallback: match by name if segment labelling differs
    for r in kx._instruments(kite, exch):
        if r.get("tradingsymbol") == want:
            return int(r["instrument_token"])
    raise RuntimeError(f"no Kite spot instrument for {index} ({want} on {exch})")


def _kite_minute(kite, token: int, day: dt.date, t0: dt.time, t1: dt.time) -> pd.DataFrame:
    """1-min OHLCV for `token` between t0..t1 on `day`, as datetime/open/high/low/close/volume."""
    frm = dt.datetime.combine(day, t0)
    to = dt.datetime.combine(day, t1)
    rows = kite.historical_data(token, frm, to, "minute")
    if not rows:
        raise RuntimeError(f"no Kite history for token {token} on {day}")
    df = pd.DataFrame(rows)
    df["datetime"] = [pd.Timestamp(x).tz_convert("Asia/Kolkata").tz_localize(None)
                      if pd.Timestamp(x).tzinfo is not None else pd.Timestamp(x)
                      for x in df["date"]]
    return df[["datetime", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def _leg_915_close(kite, token: int, day: dt.date):
    """Close of the 09:15 1-min bar (what strike_selector uses for selection)."""
    try:
        df = _kite_minute(kite, token, day, dt.time(9, 15), dt.time(9, 16))
    except Exception:
        return None
    b = df[df["datetime"].dt.strftime("%H:%M") == "09:15"]
    if b.empty:
        b = df.head(1)
    return float(b.iloc[0]["close"]) if not b.empty else None


def _select(kite, index: str, day: dt.date) -> dict:
    """Mirror strike_selector: ATM from 09:15 spot, scan OTM, first combined <= threshold."""
    expiry, d_dte = dc.nearest_expiry_and_dte(index, today=day)
    # engine clamps the threshold lookup to the 1-DTE value outside {0,1} (live_capture.py);
    # an index is only a LIVE-TRADE day when dte in {0,1} (dte_calculator.tradeable gate).
    thr = ss.threshold_for(index, d_dte if d_dte in (0, 1) else 1)
    tradeable = d_dte in (0, 1)
    stoken = _index_spot_token(kite, index)
    _pace()
    sdf = _kite_minute(kite, stoken, day, dt.time(9, 15), dt.time(9, 16))
    spot = float(sdf.iloc[0]["close"])
    atm = ss.atm_strike(spot, index)
    iv = ss.STRIKE_INTERVAL[index]

    for n in range(1, ss.MAX_OTM_SCAN + 1):
        try:
            ce = kx.resolve(kite, index, expiry, atm + n * iv, "CE")
            pe = kx.resolve(kite, index, expiry, atm - n * iv, "PE")
        except Exception:
            continue
        _pace(); ce_ltp = _leg_915_close(kite, ce["instrument_token"], day)
        _pace(); pe_ltp = _leg_915_close(kite, pe["instrument_token"], day)
        if ce_ltp is None or pe_ltp is None:
            continue
        combined = ce_ltp + pe_ltp
        if combined <= thr:
            return {"index": index, "expiry": expiry.isoformat(), "dte": d_dte,
                    "tradeable": tradeable,
                    "otm_level": n, "ce": ce, "pe": pe,
                    "ce_strike": atm + n * iv, "pe_strike": atm - n * iv,
                    "spot": round(spot, 2), "atm": atm, "lot_size": int(ce["lot_size"]),
                    "combined_premium": round(combined, 2), "threshold": thr}
    raise RuntimeError(f"No strangle <= {thr} within {ss.MAX_OTM_SCAN} OTM for {index} {day} "
                       f"(spot={spot}, atm={atm}, dte={d_dte})")


def _build_record(sel: dict, dfc: pd.DataFrame, day: dt.date, version: str) -> dict:
    """Reproduce live_tick_engine.write_archive's record from the combined 5-min frame."""
    candles = [{"time": r.datetime.strftime("%H:%M"),
                "open": round(float(r.open), 2), "high": round(float(r.high), 2),
                "low": round(float(r.low), 2), "close": round(float(r.close), 2),
                "volume": int(r.volume)}
               for r in dfc.itertuples()
               if MKT_OPEN <= r.datetime.time() <= MKT_CLOSE]
    df = pd.DataFrame(candles)
    df["datetime"] = pd.to_datetime(day.isoformat() + " " + df["time"])
    df["typ"] = (df.high + df.low + df.close) / 3
    df["pv"] = df.typ * df.volume
    cv = df.volume.cumsum().replace(0, pd.NA)
    df["vwap"] = (df.pv.cumsum() / cv).ffill().fillna(df.close).round(2)
    df["is_red"] = df.close < df.open
    df["below_vwap"] = df.close < df.vwap
    df["above_vwap"] = df.close > df.vwap

    events = simulate_day(df, dte=sel["dte"])
    lot = sel["lot_size"]
    pnl = pair_trades(events, lot_size=lot, lots=1)
    pnl["mtm"] = mtm_series(df, events, lot_size=lot, lots=1)

    meta = {"index": sel["index"], "expiry": sel["expiry"], "otm_level": sel["otm_level"],
            "ce_symbol": sel["ce"]["tradingsymbol"], "pe_symbol": sel["pe"]["tradingsymbol"],
            "ce_strike": sel["ce_strike"], "pe_strike": sel["pe_strike"],
            "spot": sel["spot"], "atm": sel["atm"], "lot_size": lot,
            "combined_premium": sel["combined_premium"], "threshold": sel["threshold"],
            "dte": sel["dte"], "tradeable": sel.get("tradeable")}
    return {"date": day.isoformat(), "index": sel["index"], "version": version,
            "ce_symbol": sel["ce"]["tradingsymbol"], "pe_symbol": sel["pe"]["tradingsymbol"],
            "otm_level": sel["otm_level"],
            "captured_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "kite_history_rebuild", "n_candles": len(candles),
            "candles": [{**c, "vwap": float(df.loc[i, "vwap"])} for i, c in enumerate(candles)],
            "events": events, "pnl": pnl, "selection": meta}


def rebuild(index: str, day: dt.date) -> str:
    kite = kx.get_kite()
    sel = _select(kite, index, day)
    print(f"  [{index}] {day} selected: {sel['ce']['tradingsymbol']} + {sel['pe']['tradingsymbol']} "
          f"(otm={sel['otm_level']}, combined={sel['combined_premium']} <= {sel['threshold']}, "
          f"dte={sel['dte']}, tradeable={sel['tradeable']})")
    _pace(); ce_df = _kite_minute(kite, sel["ce"]["instrument_token"], day, MKT_OPEN, MKT_CLOSE)
    _pace(); pe_df = _kite_minute(kite, sel["pe"]["instrument_token"], day, MKT_OPEN, MKT_CLOSE)
    dfc = build_combined(ce_df, pe_df)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for version, name in (("V2", f"{day.isoformat()}_{index}_V2.json"),
                          ("V1", f"{day.isoformat()}_{index}.json")):
        rec = _build_record(sel, dfc, day, version)
        (ARCHIVE_DIR / name).write_text(json.dumps(rec, indent=2))
        print(f"      wrote {name}: {rec['n_candles']} candles, "
              f"day P&L={rec['pnl'].get('total')} (events={len(rec['events'])})")
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="YYYY-MM-DD trading day")
    ap.add_argument("--indices", nargs="+", default=["NIFTY", "SENSEX"])
    a = ap.parse_args()
    day = dt.date.fromisoformat(a.date)
    print(f"Rebuilding candle archives from Kite history for {day} :: {a.indices}")
    rc = 0
    for idx in a.indices:
        try:
            rebuild(idx, day)
        except Exception as e:
            rc = 1
            print(f"  [{idx}] FAILED: {type(e).__name__}: {e}")
    print("done." if rc == 0 else "done WITH ERRORS.")
    sys.exit(rc)


if __name__ == "__main__":
    main()
