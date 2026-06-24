"""
strangle_system/decision_runner.py
===================================
The morning trade/no-trade FLAG — deployable v1 on Layers 1+2 only.

This is the interim decision engine the build spec calls a "deployable v1"
(L1 VRP edge + L2 hard guardrails). The full weighted L5 score, strike
selection, and position sizing arrive in Phase 5 and will replace the simple
gate here. Until the VRP-validation backtest passes, this flag is for
PAPER-LOGGING only — it is emitted daily and recorded so that, once enough
snapshots accrue, we can score the signal's historical quality.

v1 logic (fail-safe — any failed gate → no-trade):
    no-trade if  L1 missing            (no chain snapshot)
              or event_veto            (high-impact event)
              or expiry-day veto       (extreme gamma)
              or STRONG_TREND          (hostile regime)
              or VRP is None           (edge unmeasurable)
              or VRP < V1_VRP_TRADE_MIN
    else trade.

Outputs: one JSON per underlying (strangle_system/flags/{u}_{date}.json),
the latest flag (flags/latest.json), and an appended row in the paper log
(flags/paper_signal_log.csv).

    python -m strangle_system.decision_runner
    python -m strangle_system.decision_runner --underlyings NIFTY
"""

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from strangle_system import config
from strangle_system.signals import Decision, DataQuality
from strangle_system.data.chain_loader import ChainLoader
from strangle_system.layers.l1_volatility import compute_l1
from strangle_system.layers.l2_guardrails import compute_l2

config.reconfigure_stdout()


def decide(underlying: str, asof: Optional[date] = None,
           loader: Optional[ChainLoader] = None) -> tuple[Decision, dict]:
    """Compute the v1 decision for one underlying. Returns (Decision, raw_signals)."""
    asof = asof or date.today()
    loader = loader or ChainLoader()
    l1 = compute_l1(underlying, asof, loader)
    l2 = compute_l2(underlying, asof, loader)

    reasons = []
    trade = True

    if l1.data_quality == DataQuality.MISSING:
        trade = False
        reasons.append("no chain snapshot (L1 missing)")
    if l2.event_veto:
        trade = False
        reasons.append(f"event veto: {l2.event_reason}")
    if l2.is_expiry_day and config.EXPIRY_DAY_VETO:
        trade = False
        reasons.append("expiry day (gamma)")
    if config.V1_VETO_STRONG_TREND and l2.trend_regime.value == "STRONG_TREND":
        trade = False
        reasons.append(f"strong trend (ADX {l2.adx:.1f})" if l2.adx else "strong trend")
    if l1.vrp is None:
        trade = False
        reasons.append("VRP unmeasurable")
    elif l1.vrp < config.V1_VRP_TRADE_MIN:
        trade = False
        reasons.append(f"VRP {l1.vrp:+.4f} < min {config.V1_VRP_TRADE_MIN}")

    if trade:
        reasons.append(
            f"VRP {l1.vrp:+.4f} (IV {l1.atm_iv:.3f} vs RV-fc {l1.rv_forecast:.3f}), "
            f"{l2.trend_regime.value}, {l2.days_to_expiry}d to expiry"
        )

    dec = Decision(
        date=str(asof), underlying=underlying, trade=bool(trade),
        verdict_reason="; ".join(reasons),
        score=None,                       # no weighted score in v1 (Phase 5)
        regime={"trend": l2.trend_regime.value,
                "iv_rank": l1.iv_rank, "data_quality": l1.data_quality.value},
        suggested={},                     # strike selection = Phase 5
        size={},                          # sizing = Phase 5
        guardrails={"event_veto": l2.event_veto, "is_expiry_day": l2.is_expiry_day,
                    "days_to_expiry": l2.days_to_expiry, "adx": l2.adx},
    )
    raw = {"l1": l1.to_dict(), "l2": {**asdict(l2),
                                      "trend_regime": l2.trend_regime.value,
                                      "data_quality": l2.data_quality.value}}
    return dec, raw


def _write_outputs(dec: Decision, raw: dict):
    config.FLAG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {**asdict(dec), "signals": raw}
    (config.FLAG_DIR / f"{dec.underlying}_{dec.date}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")
    (config.FLAG_DIR / "latest.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")

    # Append to paper log (idempotent per date+underlying)
    row = {"date": dec.date, "underlying": dec.underlying, "trade": dec.trade,
           "vrp": raw["l1"].get("vrp"), "atm_iv": raw["l1"].get("atm_iv"),
           "rv_forecast": raw["l1"].get("rv_forecast"),
           "trend": dec.regime.get("trend"), "dte": dec.guardrails.get("days_to_expiry"),
           "reason": dec.verdict_reason}
    log = config.PAPER_LOG_FILE
    df_new = pd.DataFrame([row])
    if log.exists():
        old = pd.read_csv(log)
        old = old[~((old["date"] == dec.date) & (old["underlying"] == dec.underlying))]
        df_new = pd.concat([old, df_new], ignore_index=True)
    df_new.to_csv(log, index=False)


def run(underlyings: list[str] = None, asof: Optional[date] = None, write: bool = True):
    underlyings = underlyings or config.ACTIVE_UNDERLYINGS
    loader = ChainLoader()
    print(f"\n{'='*64}\n  STRANGLE DECISION FLAG (v1: L1+L2)  —  {asof or date.today()}\n{'='*64}")
    out = []
    for u in underlyings:
        dec, raw = decide(u, asof, loader)
        if write:
            _write_outputs(dec, raw)
        flag = "TRADE" if dec.trade else "NO-TRADE"
        print(f"\n  [{flag}] {u}")
        print(f"    {dec.verdict_reason}")
        out.append(dec)
    print(f"\n{'='*64}")
    print("  (v1 is PAPER-LOG only until the VRP-validation backtest passes.)")
    print(f"{'='*64}\n")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Strangle morning flag (v1 L1+L2)")
    ap.add_argument("--underlyings", nargs="+", default=None)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    run(underlyings=args.underlyings, write=not args.no_write)
