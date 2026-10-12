"""Offline smoke test of the Nifty ST Directional engine on the VPS.

Needs no broker login: Kite's instrument dump is public. Checks the engine imports,
resolves next session's expiry / lot size / NIFTY token exactly as the engine will,
and that the read-only API router imports.

    .venv\\Scripts\\python.exe tools\\vps\\smoke_stdir.py [--date 2026-10-12]
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import argparse
import datetime as dt
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import live_trading_options.nifty_st_directional.engine as LE   # noqa: E402
import deployment.stdir_api as api                              # noqa: E402,F401


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    day = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    eng = LE.Engine(clock=lambda: dt.datetime.combine(day, dt.time(9, 10)))
    kx = LE._load("live_trading_options/strangle_strategy/live/kite_executor.py", "_smoke_kite")
    from kiteconnect import KiteConnect
    k = KiteConnect(api_key=kx._API_KEY)
    nfo = k.instruments("NFO")
    rows = [r for r in nfo if r.get("name") == "NIFTY" and r.get("instrument_type") in ("CE", "PE")]
    eng.expiries = sorted({r["expiry"] for r in rows if r["expiry"] >= day})
    eng.choose_expiry()
    sel = [r for r in rows if r["expiry"] == eng.trade_expiry]
    lot = sel[0]["lot_size"]
    spot = [r for r in k.instruments("NSE") if r.get("tradingsymbol") == "NIFTY 50"
            and r.get("segment") == "INDICES"]
    print(f"session {day}: expiries {[str(e) for e in eng.expiries[:3]]}")
    print(f"trade expiry {eng.trade_expiry}  ({eng.expiry_reason})")
    print(f"contracts {len(sel)}  lot {lot}  qty {lot * eng.p['lots']}  NIFTY token "
          f"{spot[0]['instrument_token'] if spot else 'MISSING'}")
    ex = sel[0]["tradingsymbol"]
    print(f"sample symbol {ex} -> Fyers NSE:{ex}")
    print("SMOKE OK" if spot and lot else "SMOKE FAILED")


if __name__ == "__main__":
    main()
