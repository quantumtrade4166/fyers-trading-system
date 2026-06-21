"""
signal_engine.py
Loads historical daily closes from parquet files, computes rolling OLS beta,
z-score, half-life, and spread for each pair. Supports intraday override
so live prices can replace today's close without touching disk.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

import os
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", "G:/fyers_data_pipeline/Nifty 500 Daily Data"))

EXIT_Z   = 0.5
COOLDOWN = 5

# ── collect all unique symbols ────────────────────────────────────────────────
from deployment.pair_config import PAIRS, NAME, SYM_A, QTY_A, SYM_B, QTY_B, LOOKBACK, ENTRY_Z, STOP_Z, ANNUAL_STOP, SPAN_FACTOR

ALL_SYMS = list({p[SYM_A] for p in PAIRS} | {p[SYM_B] for p in PAIRS})


# ── data loading ─────────────────────────────────────────────────────────────

def load_prices(symbols: list[str] = ALL_SYMS) -> dict[str, pd.Series]:
    """Load daily close price series from parquet files."""
    prices = {}
    for sym in symbols:
        path = DATA_DIR / f"{sym}.parquet"
        if not path.exists():
            print(f"  [WARN] {sym}.parquet not found")
            continue
        df = pd.read_parquet(path)
        s  = df["close"].copy()
        s.index = pd.to_datetime(s.index)
        prices[sym] = s.sort_index().dropna()
    return prices


# ── OLS helpers ───────────────────────────────────────────────────────────────

def _ols_beta(pa: np.ndarray, pb: np.ndarray) -> float:
    try:
        return OLS(pa, add_constant(pb)).fit().params[1]
    except Exception:
        return np.nan


def _half_life(spread: np.ndarray) -> float:
    try:
        phi = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
        return -np.log(2) / np.log(1 + phi) if phi < 0 else 999.0
    except Exception:
        return 999.0


# ── per-pair startup stats (computed once at launch) ─────────────────────────

def compute_max_hl(pa_all: np.ndarray, pb_all: np.ndarray) -> float:
    """Full-history half-life × 2 — entry gate."""
    beta   = _ols_beta(pa_all, pb_all)
    spread = pa_all - beta * pb_all
    return _half_life(spread) * 2.0


# ── rolling z-score for one pair ─────────────────────────────────────────────

def compute_pair_zscore(
    pa_series: pd.Series,
    pb_series: pd.Series,
    lookback: int,
    today_prices: dict | None = None,
) -> dict:
    """
    Compute rolling z-score using last `lookback` days.
    today_prices = {sym_A: live_price, sym_B: live_price} injects today's close.
    Returns dict with z, beta, spread_today, hl, spread_history (last 60 pts).
    """
    aligned = pd.DataFrame({"A": pa_series, "B": pb_series}).dropna()

    if today_prices:
        sym_a = pa_series.name
        sym_b = pb_series.name
        today = pd.Timestamp.now().normalize()
        if today not in aligned.index:
            row = pd.DataFrame(
                {"A": [today_prices.get(sym_a, aligned["A"].iloc[-1])],
                 "B": [today_prices.get(sym_b, aligned["B"].iloc[-1])]},
                index=[today],
            )
            aligned = pd.concat([aligned, row])

    if len(aligned) < lookback + 2:
        return {"z": np.nan, "beta": np.nan, "spread_today": np.nan,
                "hl": np.nan, "spread_history": [], "error": "insufficient data"}

    window = aligned.iloc[-(lookback):]
    pa_w   = window["A"].values
    pb_w   = window["B"].values

    beta     = _ols_beta(pa_w, pb_w)
    if np.isnan(beta):
        return {"z": np.nan, "beta": np.nan, "spread_today": np.nan,
                "hl": np.nan, "spread_history": [], "error": "OLS failed"}

    spread_w     = pa_w - beta * pb_w
    hl           = _half_life(spread_w)
    sp_today     = pa_w[-1] - beta * pb_w[-1]
    mu, sig      = spread_w[:-1].mean(), spread_w[:-1].std()
    z            = (sp_today - mu) / sig if sig > 0 else 0.0

    hist_window  = aligned.iloc[-60:]
    pa_h, pb_h   = hist_window["A"].values, hist_window["B"].values

    spread_hist  = []
    for i in range(len(hist_window)):
        end = min(i + 1, len(hist_window))
        start = max(0, end - lookback)
        if end - start < 10:
            spread_hist.append(None)
            continue
        b = _ols_beta(pa_h[start:end], pb_h[start:end])
        if np.isnan(b):
            spread_hist.append(None)
            continue
        sw   = pa_h[start:end] - b * pb_h[start:end]
        sp_i = pa_h[end - 1] - b * pb_h[end - 1]
        m, s = sw[:-1].mean(), sw[:-1].std()
        spread_hist.append(round(float((sp_i - m) / s), 4) if s > 0 else 0.0)

    return {
        "z":              round(float(z), 4),
        "beta":           round(float(beta), 4),
        "spread_today":   round(float(sp_today), 4),
        "hl":             round(float(hl), 2),
        "spread_history": spread_hist,
        "dates":          [d.strftime("%Y-%m-%d") for d in hist_window.index],
        "error":          None,
    }


# ── main entry point ──────────────────────────────────────────────────────────

_prices_cache: dict[str, pd.Series] = {}
_max_hl_cache: dict[str, float]     = {}


def init_engine() -> None:
    """Call once at app startup to load prices and compute max_hl per pair."""
    global _prices_cache, _max_hl_cache
    print("  [signal_engine] Loading price history...")
    _prices_cache = load_prices()
    print(f"  [signal_engine] Loaded {len(_prices_cache)} symbols.")
    for p in PAIRS:
        name  = p[NAME]
        sym_a, sym_b = p[SYM_A], p[SYM_B]
        if sym_a in _prices_cache and sym_b in _prices_cache:
            pa = _prices_cache[sym_a].values
            pb = _prices_cache[sym_b].values
            n  = min(len(pa), len(pb))
            _max_hl_cache[name] = compute_max_hl(pa[-n:], pb[-n:])
            print(f"  [signal_engine] {name}: max_hl={_max_hl_cache[name]:.1f}d")


def get_all_signals(today_prices: dict | None = None) -> dict:
    """
    Compute z-scores for all 10 pairs.
    today_prices = {sym: live_price} from WebSocket (optional).
    Returns dict keyed by pair name.
    """
    results = {}
    for p in PAIRS:
        name     = p[NAME]
        sym_a    = p[SYM_A]
        sym_b    = p[SYM_B]
        lookback = p[LOOKBACK]
        entry_z  = p[ENTRY_Z]
        stop_z   = p[STOP_Z]

        if sym_a not in _prices_cache or sym_b not in _prices_cache:
            results[name] = {"error": f"missing data for {sym_a} or {sym_b}"}
            continue

        sa = _prices_cache[sym_a].rename(sym_a)
        sb = _prices_cache[sym_b].rename(sym_b)

        stats   = compute_pair_zscore(sa, sb, lookback, today_prices)
        max_hl  = _max_hl_cache.get(name, 999.0)
        z       = stats["z"]

        signal = "flat"
        if not np.isnan(z):
            if z < -entry_z:
                signal = "long_spread"
            elif z > entry_z:
                signal = "short_spread"

        hl_ok = bool((not np.isnan(stats["hl"])) and (stats["hl"] <= max_hl))

        results[name] = {
            **stats,
            "sym_a":    sym_a,
            "sym_b":    sym_b,
            "qty_a":    p[QTY_A],
            "qty_b":    p[QTY_B],
            "entry_z":  entry_z,
            "stop_z":   stop_z,
            "exit_z":   EXIT_Z,
            "max_hl":   round(max_hl, 1),
            "hl_ok":    hl_ok,
            "signal":   signal,
            "price_a":  round(float(sa.iloc[-1]), 2) if today_prices is None else round(today_prices.get(sym_a, float(sa.iloc[-1])), 2),
            "price_b":  round(float(sb.iloc[-1]), 2) if today_prices is None else round(today_prices.get(sym_b, float(sb.iloc[-1])), 2),
        }

    return results


def reload_prices() -> None:
    """Reload parquet files from disk (call after EOD download completes)."""
    global _prices_cache
    _prices_cache = load_prices()
    print("  [signal_engine] Prices reloaded from disk.")
