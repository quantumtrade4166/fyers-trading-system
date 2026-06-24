"""
strangle_system/layers/l1_volatility.py
========================================
LAYER 1 — the volatility edge. This is the core; everything else refines it.

Produces, point-in-time for a date T:
  - realized vol (close-to-close, Garman-Klass, Yang-Zhang) over 5/10/20 days
  - an RV *forecast* over the holding horizon (EWMA baseline + GARCH(1,1))
  - ATM implied vol from the option-chain snapshot (real IV from Fyers greeks)
  - VRP = ATM_IV − forecast_RV   (the key number)
  - IV rank / percentile over the trailing window

UNITS: all vols are annualized DECIMAL fractions (0.12 = 12%). Fyers reports IV
in percent (12.73) → divided by 100 here so IV and RV are directly comparable.

POINT-IN-TIME: realized vol for T uses index bars up to T; ATM IV uses the
snapshot captured at/before T (via ChainLoader.snapshot_asof). No look-ahead.

DEGRADED MODES (fail-safe, never fabricate):
  - SENSEX spot history is not in the local data tree yet → RV/forecast/VRP
    unavailable → data_quality=DEGRADED (IV-only).
  - < min snapshots → IV rank INSUFFICIENT (this is expected for months under
    forward-accumulation; we still emit live VRP and paper-log it).
"""

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.append(str(_PROJECT_ROOT))

from strangle_system import config
from strangle_system.signals import VolatilitySignal, DataQuality
from strangle_system.data.chain_loader import ChainLoader

_ANNUAL = np.sqrt(config.TRADING_DAYS_PER_YEAR)
_MIN_SNAPSHOTS_FOR_RANK = 20   # below this, IV rank is INSUFFICIENT


# ──────────────────────────────────────────────────────────────────────────
# Daily OHLC (from existing 5-min index data)
# ──────────────────────────────────────────────────────────────────────────
def daily_ohlc(underlying: str,
               end: Optional[date] = None,
               lookback_days: int = 400) -> Optional[pd.DataFrame]:
    """
    Build daily OHLC for an underlying's spot index from the existing 5-min
    Parquet store (backtesting.DataLoader). Returns None if not available
    (e.g. SENSEX, whose BSE spot isn't in the local tree yet).

    Columns: open, high, low, close ; DatetimeIndex (daily).
    """
    cfg = config.UNDERLYINGS.get(underlying, {})
    sym = cfg.get("index_symbol")
    if not sym:
        return None

    daily = None
    # Preferred: resample 5-min history from the main data tree (e.g. NIFTY).
    try:
        from backtesting.data_loader import DataLoader
        loader = DataLoader()
        if sym in loader.available_symbols():
            df = loader.load(sym)
            if df is not None and not df.empty:
                g = df.groupby(df.index.date)
                daily = pd.DataFrame({
                    "open": g["open"].first(),
                    "high": g["high"].max(),
                    "low":  g["low"].min(),
                    "close": g["close"].last(),
                })
                daily.index = pd.to_datetime(daily.index)
    except Exception:
        daily = None

    # Fallback: backfilled daily spot file (e.g. SENSEX/BSE).
    if daily is None or daily.empty:
        try:
            from strangle_system.data.spot_backfill import load_spot_daily
            daily = load_spot_daily(underlying)
        except Exception:
            daily = None

    if daily is None or daily.empty:
        return None

    if end is not None:
        daily = daily[daily.index.date <= end]    # point-in-time cut
    if daily.empty:
        return None
    return daily.tail(lookback_days)


# ──────────────────────────────────────────────────────────────────────────
# Realized-vol estimators (annualized decimal)
# ──────────────────────────────────────────────────────────────────────────
def rv_close_to_close(ohlc: pd.DataFrame, window: int) -> Optional[float]:
    c = ohlc["close"].astype(float)
    r = np.log(c / c.shift(1)).dropna()
    if len(r) < window:
        return None
    return float(r.tail(window).std(ddof=1) * _ANNUAL)


def rv_garman_klass(ohlc: pd.DataFrame, window: int) -> Optional[float]:
    o, h, l, c = (ohlc[x].astype(float) for x in ("open", "high", "low", "close"))
    hl = np.log(h / l) ** 2
    co = np.log(c / o) ** 2
    daily_var = 0.5 * hl - (2 * np.log(2) - 1) * co
    daily_var = daily_var.dropna()
    if len(daily_var) < window:
        return None
    return float(np.sqrt(daily_var.tail(window).mean() * config.TRADING_DAYS_PER_YEAR))


def rv_yang_zhang(ohlc: pd.DataFrame, window: int) -> Optional[float]:
    """Yang-Zhang: handles overnight gaps; preferred default."""
    o, h, l, c = (ohlc[x].astype(float) for x in ("open", "high", "low", "close"))
    cc_prev = c.shift(1)
    o_ret = np.log(o / cc_prev)          # overnight
    c_ret = np.log(c / o)                # open-to-close
    # Rogers-Satchell (drift-independent)
    rs = (np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o))

    frame = pd.DataFrame({"o": o_ret, "c": c_ret, "rs": rs}).dropna()
    if len(frame) < window + 1:
        return None
    frame = frame.tail(window)
    n = len(frame)
    var_o = frame["o"].var(ddof=1)
    var_c = frame["c"].var(ddof=1)
    var_rs = frame["rs"].mean()
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    yz_var = var_o + k * var_c + (1 - k) * var_rs
    if yz_var <= 0:
        return None
    return float(np.sqrt(yz_var * config.TRADING_DAYS_PER_YEAR))


_RV_FUNCS = {
    "close_to_close": rv_close_to_close,
    "garman_klass": rv_garman_klass,
    "yang_zhang": rv_yang_zhang,
}


def realized_vol(ohlc: pd.DataFrame, window: int, method: str = None) -> Optional[float]:
    method = method or config.RV_DEFAULT_ESTIMATOR
    return _RV_FUNCS[method](ohlc, window)


# ──────────────────────────────────────────────────────────────────────────
# RV forecast — EWMA baseline + GARCH(1,1) behind a clean interface
# ──────────────────────────────────────────────────────────────────────────
def forecast_ewma(ohlc: pd.DataFrame, lam: float = None) -> Optional[float]:
    """RiskMetrics EWMA one-step variance → annualized vol."""
    lam = config.EWMA_LAMBDA if lam is None else lam
    c = ohlc["close"].astype(float)
    r = np.log(c / c.shift(1)).dropna().values
    if len(r) < 5:
        return None
    var = np.var(r)              # seed
    for x in r:
        var = lam * var + (1 - lam) * x * x
    return float(np.sqrt(var) * _ANNUAL)


def forecast_garch(ohlc: pd.DataFrame, horizon: int = 1) -> Optional[float]:
    """
    GARCH(1,1) average-variance forecast over `horizon` days → annualized vol.
    Returns None if arch unavailable or the fit fails (fail-safe).
    """
    try:
        from arch import arch_model
    except Exception:
        return None
    c = ohlc["close"].astype(float)
    r = (np.log(c / c.shift(1)).dropna() * 100.0)   # arch likes %-scale
    if len(r) < 50:
        return None
    try:
        res = arch_model(r, mean="Constant", vol="Garch", p=1, q=1, dist="normal").fit(disp="off")
        fc = res.forecast(horizon=max(1, horizon), reindex=False)
        var_pct = float(np.mean(fc.variance.values[-1]))   # avg daily variance (%^2)
        daily_vol = np.sqrt(var_pct) / 100.0
        return float(daily_vol * _ANNUAL)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────
# ATM IV from a chain snapshot
# ──────────────────────────────────────────────────────────────────────────
def atm_iv_from_snapshot(snapshot: pd.DataFrame, expiry: Optional[str] = None) -> Optional[float]:
    """
    ATM implied vol (annualized decimal) for one expiry, interpolated to spot.
    Averages CE & PE IV per strike, then linearly interpolates between the two
    strikes bracketing spot. Fyers IV is in percent → /100.
    """
    sl = ChainLoader.expiry_slice(snapshot, expiry)
    spot = ChainLoader.spot(snapshot)
    if sl.empty or spot is None:
        return None
    per_strike = (sl.dropna(subset=["iv"])
                    .groupby("strike")["iv"].mean()
                    .sort_index())
    if per_strike.empty:
        return None
    strikes = per_strike.index.values.astype(float)
    ivs = per_strike.values.astype(float)
    iv_pct = float(np.interp(spot, strikes, ivs))   # clamps outside range
    return iv_pct / 100.0


def atm_iv_history(loader: ChainLoader, underlying: str,
                   asof: date, expiry_mode: str = "nearest") -> pd.Series:
    """ATM IV (decimal) for every snapshot up to and including `asof`."""
    out = {}
    for d in loader.available_dates(underlying):
        if d > asof:
            continue
        snap = loader.load_snapshot(underlying, d)
        if snap is None or snap.empty:
            continue
        iv = atm_iv_from_snapshot(snap)   # nearest expiry
        if iv is not None:
            out[d] = iv
    return pd.Series(out).sort_index()


def iv_rank_percentile(iv_series: pd.Series, current: float) -> tuple[Optional[float], Optional[float]]:
    """IV rank (range position) and percentile over the series. None if too short."""
    s = iv_series.dropna()
    if len(s) < _MIN_SNAPSHOTS_FOR_RANK:
        return None, None
    lo, hi = float(s.min()), float(s.max())
    rank = 100.0 * (current - lo) / (hi - lo) if hi > lo else 50.0
    pct = 100.0 * float((s <= current).mean())
    return round(rank, 2), round(pct, 2)


# ──────────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────────
def compute_l1(underlying: str, asof: Optional[date] = None,
               loader: Optional[ChainLoader] = None) -> VolatilitySignal:
    """Assemble the Layer-1 VolatilitySignal for `underlying` as of `asof`."""
    asof = asof or date.today()
    loader = loader or ChainLoader()
    sig = VolatilitySignal(underlying=underlying, asof=str(asof))

    snapshot = loader.snapshot_asof(underlying, asof)
    if snapshot is None or snapshot.empty:
        sig.data_quality = DataQuality.MISSING
        sig.notes = "No option-chain snapshot at/before asof — fail safe (no-trade)."
        return sig

    expiry = ChainLoader.nearest_expiry(snapshot)
    sig.atm_iv = atm_iv_from_snapshot(snapshot, expiry)
    if expiry:
        try:
            sig.horizon_days = max(1, (pd.Timestamp(expiry).date() - asof).days)
        except Exception:
            sig.horizon_days = None

    # Realized vol + forecast (needs spot OHLC history)
    ohlc = daily_ohlc(underlying, end=asof)
    notes = []
    if ohlc is not None and len(ohlc) > 25:
        sig.rv_5 = realized_vol(ohlc, 5)
        sig.rv_10 = realized_vol(ohlc, 10)
        sig.rv_20 = realized_vol(ohlc, 20)
        garch = forecast_garch(ohlc, horizon=sig.horizon_days or 1)
        if garch is not None:
            sig.rv_forecast, sig.rv_forecast_method = garch, "garch"
        else:
            sig.rv_forecast, sig.rv_forecast_method = forecast_ewma(ohlc), "ewma"
    else:
        notes.append(f"No spot OHLC history for {underlying} — RV/forecast unavailable.")

    # VRP
    if sig.atm_iv is not None and sig.rv_forecast is not None:
        sig.vrp = round(sig.atm_iv - sig.rv_forecast, 4)

    # IV rank / percentile (degraded under forward-accumulation)
    if sig.atm_iv is not None:
        hist = atm_iv_history(loader, underlying, asof)
        sig.iv_rank, sig.iv_percentile = iv_rank_percentile(hist, sig.atm_iv)
        if sig.iv_rank is None:
            notes.append(f"IV rank insufficient ({len(hist)}/{_MIN_SNAPSHOTS_FOR_RANK} snapshots).")

    # Data-quality verdict
    if sig.atm_iv is None:
        sig.data_quality = DataQuality.MISSING
    elif sig.vrp is None or sig.iv_rank is None:
        sig.data_quality = DataQuality.DEGRADED
    else:
        sig.data_quality = DataQuality.OK
    sig.notes = " ".join(notes)
    return sig


if __name__ == "__main__":
    config.reconfigure_stdout()
    for u in config.ACTIVE_UNDERLYINGS:
        s = compute_l1(u)
        print(f"\n=== L1 / {u} ({s.asof}) ===")
        for k, v in s.to_dict().items():
            print(f"  {k:18}: {v}")
