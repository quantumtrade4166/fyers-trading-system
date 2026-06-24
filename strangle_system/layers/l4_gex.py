"""
strangle_system/layers/l4_gex.py
================================
LAYER 4 — Gamma Exposure (GEX) regime filter.  *** GATED ***

Builds the GEX concept correctly, but it is FORBIDDEN from influencing any live
decision until gex_validation.py proves it separates realized vol on Nifty.
Until then config.GEX_VALIDATED is False → GexSignal.validated=False → L5 weight
w5 = 0. This module computes and visualizes GEX; it does not gate trades.

Per-strike gamma exposure (uses the real per-strike gamma from greeks=1):

    strike_gex = gamma × open_interest × lot_size × spot² × 0.01

CONVENTION (state plainly): calls contribute POSITIVE, puts NEGATIVE — the
standard dealers-are-net-short-customer-options assumption. This is an ESTIMATE;
vendors differ; it is UNVALIDATED for Nifty until the backtest proves regime
separation. Gamma is positive for both calls and puts, so the sign comes purely
from the call/put convention here.

Key levels:
  - gamma flip : strike where cumulative net GEX crosses zero (interpolated).
                 This is a recognized PROXY for the zero-gamma level, not the
                 full re-priced-spot computation — also unvalidated.
  - call wall  : strike with the largest positive (call) gamma×OI.
  - put wall   : strike with the largest (put) gamma×OI (most negative leg).
Regime: spot ≥ flip → POSITIVE (dealers dampen → favorable); else NEGATIVE.
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
from strangle_system.signals import GexSignal, GexRegime, DataQuality
from strangle_system.data.chain_loader import ChainLoader


def per_strike_gex(snapshot: pd.DataFrame, underlying: str,
                   expiry: Optional[str] = None) -> pd.DataFrame:
    """
    Per-strike GEX for one expiry. Returns DataFrame:
        strike, call_gex (+), put_gex (−), net_gex
    sorted by strike. Empty if inputs missing.
    """
    sl = ChainLoader.expiry_slice(snapshot, expiry)
    spot = ChainLoader.spot(snapshot)
    lot = config.UNDERLYINGS.get(underlying, {}).get("lot_size")
    if sl.empty or spot is None or not lot:
        return pd.DataFrame(columns=["strike", "call_gex", "put_gex", "net_gex"])

    scale = lot * (spot ** 2) * 0.01
    ce = (sl[sl["opt_type"] == "CE"].dropna(subset=["gamma", "oi"])
          .assign(g=lambda d: d["gamma"] * d["oi"] * scale)
          .groupby("strike")["g"].sum())
    pe = (sl[sl["opt_type"] == "PE"].dropna(subset=["gamma", "oi"])
          .assign(g=lambda d: d["gamma"] * d["oi"] * scale)
          .groupby("strike")["g"].sum())
    strikes = sorted(set(ce.index) | set(pe.index))
    if not strikes:
        return pd.DataFrame(columns=["strike", "call_gex", "put_gex", "net_gex"])
    df = pd.DataFrame({"strike": strikes})
    df["call_gex"] = df["strike"].map(ce).fillna(0.0)
    df["put_gex"] = -df["strike"].map(pe).fillna(0.0)      # puts negative
    df["net_gex"] = df["call_gex"] + df["put_gex"]
    return df.reset_index(drop=True)


def _gamma_flip(profile: pd.DataFrame) -> Optional[float]:
    """Strike level where CUMULATIVE net GEX crosses zero (interpolated proxy)."""
    if profile.empty:
        return None
    cum = profile["net_gex"].cumsum().values
    strikes = profile["strike"].values.astype(float)
    for i in range(1, len(cum)):
        if cum[i - 1] == 0:
            return float(strikes[i - 1])
        if cum[i - 1] < 0 <= cum[i] or cum[i - 1] > 0 >= cum[i]:
            # linear interpolate the zero crossing between strikes i-1 and i
            x0, x1, y0, y1 = strikes[i - 1], strikes[i], cum[i - 1], cum[i]
            if y1 == y0:
                return float(x1)
            return float(x0 + (0 - y0) * (x1 - x0) / (y1 - y0))
    return None      # no crossing


def compute_l4(underlying: str, asof: Optional[date] = None,
               loader: Optional[ChainLoader] = None) -> GexSignal:
    asof = asof or date.today()
    loader = loader or ChainLoader()
    sig = GexSignal(underlying=underlying, asof=str(asof),
                    validated=bool(config.GEX_VALIDATED))   # gate

    snapshot = loader.snapshot_asof(underlying, asof)
    if snapshot is None or snapshot.empty:
        sig.data_quality = DataQuality.MISSING
        return sig

    expiry = ChainLoader.nearest_expiry(snapshot) if config.GEX_EXPIRY_MODE == "nearest" else None
    profile = per_strike_gex(snapshot, underlying, expiry)
    spot = ChainLoader.spot(snapshot)
    if profile.empty or spot is None:
        sig.data_quality = DataQuality.DEGRADED
        return sig

    sig.net_gex = float(profile["net_gex"].sum())
    sig.gamma_flip = _gamma_flip(profile)
    sig.call_wall = float(profile.loc[profile["call_gex"].idxmax(), "strike"])
    sig.put_wall = float(profile.loc[profile["put_gex"].idxmin(), "strike"])   # most negative
    if sig.gamma_flip is not None:
        sig.gex_regime = GexRegime.POSITIVE if spot >= sig.gamma_flip else GexRegime.NEGATIVE
    else:
        sig.gex_regime = GexRegime.POSITIVE if sig.net_gex >= 0 else GexRegime.NEGATIVE
    return sig


def gex_profile_for_chart(underlying: str, asof: Optional[date] = None,
                          loader: Optional[ChainLoader] = None) -> list[dict]:
    """Per-strike net GEX list for the dashboard chart."""
    loader = loader or ChainLoader()
    snap = loader.snapshot_asof(underlying, asof or date.today())
    if snap is None or snap.empty:
        return []
    prof = per_strike_gex(snap, underlying, ChainLoader.nearest_expiry(snap))
    return [{"strike": float(r.strike), "net_gex": float(r.net_gex)} for r in prof.itertuples()]


if __name__ == "__main__":
    config.reconfigure_stdout()
    for u in config.ACTIVE_UNDERLYINGS:
        s = compute_l4(u)
        print(f"\n=== L4 GEX / {u} ({s.asof}) ===")
        print(f"  net_gex    : {s.net_gex:,.0f}" if s.net_gex is not None else "  net_gex    : None")
        print(f"  gamma_flip : {s.gamma_flip}")
        print(f"  call_wall  : {s.call_wall}   put_wall: {s.put_wall}")
        print(f"  regime     : {s.gex_regime.value if s.gex_regime else None}")
        print(f"  validated  : {s.validated}   (gated off decisions until True)")
        print(f"  data_quality: {s.data_quality.value}")
