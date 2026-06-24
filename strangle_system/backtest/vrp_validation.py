"""
strangle_system/backtest/vrp_validation.py
===========================================
THE PHASE-1 GATE: does a higher VRP at entry predict a better short-vol outcome?

Method (point-in-time, no look-ahead):
  For each snapshot date d:
    - entry VRP and ATM IV (sold over the holding horizon, to nearest expiry)
    - forward realized vol over the next `horizon` trading days (from index OHLC
      AFTER d only)
    - premium_capture = ATM_IV − forward_RV
        > 0  → IV sold exceeded realized vol  → favorable for a short strangle
  Bucket days by entry VRP; the mean premium_capture per bucket must rise
  monotonically with VRP, or the core premise is weak — report it honestly.

NOTE ON SCOPE: premium_capture (IV − realized) is the leading indicator of
short-strangle profitability absent a wing-breaching move. The full CASH PnL
simulation (slippage, STT, brokerage, gap/wing risk) lives in
strangle_backtest.py (Phase 5). This harness validates the EDGE; that one
validates the strategy.

DATA REALITY: under the user's "forward-accumulate only" choice there is no
historical IV. Until enough daily snapshots accrue, this runs in
INSUFFICIENT-HISTORY mode and says so plainly — it does not fabricate a result.
"""

import sys
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from strangle_system import config
from strangle_system.data.chain_loader import ChainLoader
from strangle_system.layers import l1_volatility as l1

# Minimum usable (date, outcome) rows before a bucketed result is meaningful.
MIN_USABLE_ROWS = 40


def _forward_realized_vol(daily_close: pd.Series, entry: date, horizon: int) -> Optional[float]:
    """Annualized close-to-close RV over the `horizon` trading days AFTER entry."""
    fwd = daily_close[daily_close.index.date > entry].head(horizon)
    if len(fwd) < max(3, horizon // 2):
        return None
    # include the entry close so the first forward return is measured
    anchor = daily_close[daily_close.index.date <= entry]
    if anchor.empty:
        return None
    series = pd.concat([anchor.tail(1), fwd])
    r = np.log(series / series.shift(1)).dropna()
    if len(r) < 2:
        return None
    return float(r.std(ddof=1) * np.sqrt(config.TRADING_DAYS_PER_YEAR))


def assemble_table(underlying: str, loader: Optional[ChainLoader] = None) -> pd.DataFrame:
    """Build the per-snapshot validation table (entry VRP/IV + forward RV)."""
    loader = loader or ChainLoader()
    dates = loader.available_dates(underlying)
    rows = []

    ohlc = l1.daily_ohlc(underlying, end=date.today(), lookback_days=10000)
    daily_close = ohlc["close"] if ohlc is not None else None

    for d in dates:
        snap = loader.load_snapshot(underlying, d)
        if snap is None or snap.empty:
            continue
        expiry = ChainLoader.nearest_expiry(snap)
        atm_iv = l1.atm_iv_from_snapshot(snap, expiry)
        if atm_iv is None:
            continue
        horizon = 1
        if expiry:
            try:
                horizon = max(1, (pd.Timestamp(expiry).date() - d).days)
            except Exception:
                horizon = 1

        # entry VRP needs a forecast that uses ONLY data up to d (point-in-time)
        vrp = fwd_rv = prem = None
        if ohlc is not None:
            pit = ohlc[ohlc.index.date <= d]
            if len(pit) > 50:
                fc = l1.forecast_garch(pit, horizon=horizon) or l1.forecast_ewma(pit)
                if fc is not None:
                    vrp = atm_iv - fc
                fwd_rv = _forward_realized_vol(daily_close, d, horizon)
                if fwd_rv is not None:
                    prem = atm_iv - fwd_rv

        rows.append({"date": str(d), "atm_iv": atm_iv, "vrp": vrp,
                     "horizon": horizon, "fwd_rv": fwd_rv, "premium_capture": prem})
    return pd.DataFrame(rows)


def run_validation(underlying: str = "NIFTY", n_buckets: int = 4) -> dict:
    print(f"\n{'='*64}\n  VRP VALIDATION — {underlying}\n{'='*64}")
    tbl = assemble_table(underlying)
    usable = tbl.dropna(subset=["vrp", "premium_capture"]) if not tbl.empty else tbl

    print(f"  Snapshots on disk        : {len(tbl)}")
    print(f"  Usable (VRP + fwd RV)    : {len(usable)}")
    print(f"  Required for a verdict   : {MIN_USABLE_ROWS}")

    if len(usable) < MIN_USABLE_ROWS:
        need = MIN_USABLE_ROWS - len(usable)
        print(f"\n  STATUS: INSUFFICIENT HISTORY")
        print(f"  Forward-accumulation in progress — need ~{need} more usable trading")
        print(f"  days of snapshots before VRP predictiveness can be tested.")
        print(f"  (Live VRP is still emitted daily by Layer 1 and should be paper-logged.)")
        print(f"{'='*64}\n")
        return {"underlying": underlying, "status": "insufficient_history",
                "snapshots": len(tbl), "usable": len(usable),
                "needed": MIN_USABLE_ROWS}

    # Enough data → bucket by entry VRP and show monotonicity of premium_capture
    usable = usable.copy()
    usable["bucket"] = pd.qcut(usable["vrp"], q=n_buckets, labels=False, duplicates="drop")
    grp = usable.groupby("bucket").agg(
        n=("vrp", "size"),
        vrp_lo=("vrp", "min"), vrp_hi=("vrp", "max"),
        mean_premium=("premium_capture", "mean"),
        win_rate=("premium_capture", lambda s: float((s > 0).mean())),
    )
    print("\n  VRP bucket → forward premium captured (IV − realized):")
    print(grp.to_string())
    means = grp["mean_premium"].values
    monotonic = bool(np.all(np.diff(means) >= 0))
    print(f"\n  Monotonic improvement with VRP: {monotonic}")
    print(f"{'='*64}\n")
    return {"underlying": underlying, "status": "ok", "usable": len(usable),
            "monotonic": monotonic, "buckets": grp.reset_index().to_dict("records")}


if __name__ == "__main__":
    config.reconfigure_stdout()
    for u in config.ACTIVE_UNDERLYINGS:
        run_validation(u)
