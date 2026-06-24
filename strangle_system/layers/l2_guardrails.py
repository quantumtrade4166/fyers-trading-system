"""
strangle_system/layers/l2_guardrails.py
========================================
LAYER 2 — event & regime guardrails. A cheap, hard-veto layer that prevents
blowups however rich premium looks.

  - Event calendar : scheduled high-impact events (RBI/Budget/CPI/FOMC/earnings)
    from a manually-maintained CSV. severity=high → hard veto; medium → size-down.
  - Expiry flag    : NIFTY expires Tuesday, SENSEX Thursday. Expiry-day gamma is
    extreme → treated as veto (config.EXPIRY_DAY_VETO) / separate regime.
    days_to_expiry is taken from the chain snapshot's nearest expiry when
    available (most accurate), else computed from the expiry weekday.
  - Trend regime   : ADX (Wilder) + EMA stack on daily spot. Strong, aligned
    trend is hostile to short strangles → STRONG_TREND.

Point-in-time: ADX/EMA use spot bars up to `asof`; events use the calendar row
for `asof`; expiry uses the snapshot at/before `asof`.
Fail-safe: missing inputs → conservative (no false "all clear").
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from strangle_system import config
from strangle_system.signals import GuardrailSignal, TrendRegime, DataQuality
from strangle_system.data.chain_loader import ChainLoader
from strangle_system.layers.l1_volatility import daily_ohlc


# ──────────────────────────────────────────────────────────────────────────
# Event calendar
# ──────────────────────────────────────────────────────────────────────────
def load_event_calendar() -> pd.DataFrame:
    """Read the manual event CSV (comment lines starting with # ignored)."""
    path = config.EVENT_CALENDAR_FILE
    if not path.exists():
        return pd.DataFrame(columns=["date", "event", "severity"])
    df = pd.read_csv(path, comment="#")
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["severity"] = df["severity"].astype(str).str.strip().str.lower()
    return df.dropna(subset=["date"])


def event_for(asof: date, cal: Optional[pd.DataFrame] = None) -> tuple[bool, str, str]:
    """Return (is_high_veto, severity, reason) for `asof`."""
    cal = cal if cal is not None else load_event_calendar()
    if cal.empty:
        return False, "", ""
    rows = cal[cal["date"] == asof]
    if rows.empty:
        return False, "", ""
    sev_rank = {"high": 3, "medium": 2, "low": 1}
    rows = rows.assign(_r=rows["severity"].map(sev_rank).fillna(0)).sort_values("_r", ascending=False)
    top = rows.iloc[0]
    reason = "; ".join(f"{r.event}({r.severity})" for r in rows.itertuples())
    return (top["severity"] == "high"), top["severity"], reason


# ──────────────────────────────────────────────────────────────────────────
# Expiry
# ──────────────────────────────────────────────────────────────────────────
def _next_weekday(asof: date, weekday: int) -> date:
    """Next date (>= asof) falling on `weekday` (0=Mon..6=Sun)."""
    delta = (weekday - asof.weekday()) % 7
    return asof + timedelta(days=delta)


def expiry_info(underlying: str, asof: date,
                loader: Optional[ChainLoader] = None) -> tuple[Optional[int], bool]:
    """
    (days_to_expiry, is_expiry_day). Prefer the chain snapshot's nearest expiry;
    fall back to the configured expiry weekday.
    """
    loader = loader or ChainLoader()
    snap = loader.snapshot_asof(underlying, asof)
    if snap is not None and not snap.empty:
        ne = ChainLoader.nearest_expiry(snap)
        if ne:
            try:
                exp = pd.Timestamp(ne).date()
                dte = (exp - asof).days
                return max(0, dte), (dte <= 0)
            except Exception:
                pass
    wd = config.UNDERLYINGS.get(underlying, {}).get("expiry_weekday")
    if wd is None:
        return None, False
    exp = _next_weekday(asof, wd)
    dte = (exp - asof).days
    return dte, (dte == 0)


# ──────────────────────────────────────────────────────────────────────────
# Trend regime — ADX (Wilder) + EMA stack
# ──────────────────────────────────────────────────────────────────────────
def adx(ohlc: pd.DataFrame, period: int = None) -> Optional[float]:
    """Latest Wilder ADX. None if insufficient data."""
    period = period or config.ADX_PERIOD
    if ohlc is None or len(ohlc) < period * 3:
        return None
    h, l, c = ohlc["high"], ohlc["low"], ohlc["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)

    alpha = 1.0 / period   # Wilder smoothing
    atr = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=ohlc.index).ewm(alpha=alpha, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=ohlc.index).ewm(alpha=alpha, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_series = dx.ewm(alpha=alpha, adjust=False).mean()
    val = adx_series.iloc[-1]
    return float(val) if pd.notna(val) else None


def ema_aligned(ohlc: pd.DataFrame, periods: list[int] = None) -> bool:
    """True if close and the EMA stack are monotonically aligned (clean trend)."""
    periods = sorted(periods or config.EMA_STACK)
    if ohlc is None or len(ohlc) < max(periods) + 1:
        return False
    c = ohlc["close"]
    emas = [c.ewm(span=p, adjust=False).mean().iloc[-1] for p in periods]
    last = c.iloc[-1]
    up = last > emas[0] and all(emas[i] > emas[i + 1] for i in range(len(emas) - 1))
    dn = last < emas[0] and all(emas[i] < emas[i + 1] for i in range(len(emas) - 1))
    return bool(up or dn)


def trend_regime(ohlc: pd.DataFrame) -> tuple[TrendRegime, Optional[float]]:
    a = adx(ohlc)
    if a is None:
        return TrendRegime.RANGE, None      # unknown → treat as range but flag via DQ upstream
    aligned = ema_aligned(ohlc)
    if a >= config.ADX_TREND_MIN and aligned:
        return TrendRegime.STRONG_TREND, a
    if a >= config.ADX_RANGE_MAX:
        return TrendRegime.WEAK_TREND, a
    return TrendRegime.RANGE, a


# ──────────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────────
def compute_l2(underlying: str, asof: Optional[date] = None,
               loader: Optional[ChainLoader] = None) -> GuardrailSignal:
    asof = asof or date.today()
    loader = loader or ChainLoader()
    sig = GuardrailSignal(underlying=underlying, asof=str(asof))

    veto, _sev, reason = event_for(asof)
    sig.event_veto, sig.event_reason = veto, reason

    dte, is_exp = expiry_info(underlying, asof, loader)
    sig.days_to_expiry, sig.is_expiry_day = dte, is_exp

    ohlc = daily_ohlc(underlying, end=asof)
    sig.trend_regime, sig.adx = trend_regime(ohlc)
    if sig.adx is None:
        sig.data_quality = DataQuality.DEGRADED
    return sig


if __name__ == "__main__":
    config.reconfigure_stdout()
    for u in config.ACTIVE_UNDERLYINGS:
        s = compute_l2(u)
        print(f"\n=== L2 / {u} ({s.asof}) ===")
        print(f"  event_veto    : {s.event_veto}  reason='{s.event_reason}'")
        print(f"  is_expiry_day : {s.is_expiry_day}  days_to_expiry: {s.days_to_expiry}")
        print(f"  trend_regime  : {s.trend_regime.value}  adx: {s.adx}")
        print(f"  data_quality  : {s.data_quality.value}")
