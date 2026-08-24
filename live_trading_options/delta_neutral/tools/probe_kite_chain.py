"""
tools/probe_kite_chain.py — confirm Kite can supply everything the chain needs.
==============================================================================

The delta-neutral engine takes its market data from Kite (not Fyers), so that it
never competes with the VWAP strangle's Fyers socket and so its prices come from
the same venue it places orders on.

This proves, against the live instrument dump, that Kite gives us:
  - a spot instrument token per index (NIFTY 50 / SENSEX)
  - the full option chain for the front expiry, with strike + type + lot size
  - a KiteTicker we can subscribe those tokens on

Read-only. Never places an order. Run it on the VPS, where the daily Kite token
lives.

    python live_trading_options/delta_neutral/tools/probe_kite_chain.py
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from live.broker import kite_executor as kx

# what we hope to find as the spot instrument for each index
SPOT_CANDIDATES = {
    "NIFTY":  ("NSE", ("NIFTY 50", "NIFTY50")),
    "SENSEX": ("BSE", ("SENSEX", "BSE SENSEX")),
}
OPT_EXCHANGE = {"NIFTY": "NFO", "SENSEX": "BFO"}


def main():
    k = kx.get_kite()
    print(f"  kite auth ok: {k.profile().get('user_id')}")
    try:
        from kiteconnect import KiteTicker            # noqa: F401
        print("  KiteTicker importable: yes")
    except Exception as e:
        print(f"  KiteTicker importable: NO — {e}")

    for index, (exch, names) in SPOT_CANDIDATES.items():
        print(f"\n  ── {index} ──")
        try:
            dump = k.instruments(exch)
        except Exception as e:
            print(f"    {exch} dump failed: {e}")
            continue
        hits = [r for r in dump
                if r.get("tradingsymbol") in names or
                (r.get("segment") == "INDICES" and index in str(r.get("tradingsymbol", "")).upper())]
        if not hits:
            print(f"    NO spot instrument found in {exch} for {names}")
            idx_rows = [r for r in dump if r.get("segment") == "INDICES"][:8]
            print("    (sample INDICES rows:",
                  [r.get("tradingsymbol") for r in idx_rows], ")")
        for r in hits[:3]:
            print(f"    SPOT  {r['tradingsymbol']:12s} token={r['instrument_token']:<12} "
                  f"segment={r.get('segment')}  exch={r.get('exchange')}")

        oe = OPT_EXCHANGE[index]
        try:
            opts = [r for r in k.instruments(oe)
                    if r.get("name") == index and r.get("instrument_type") in ("CE", "PE")]
        except Exception as e:
            print(f"    {oe} dump failed: {e}")
            continue
        if not opts:
            print(f"    NO options found in {oe} for name={index}")
            continue
        exps = sorted({r["expiry"] for r in opts})
        front = exps[0]
        chain = [r for r in opts if r["expiry"] == front]
        strikes = sorted({int(r["strike"]) for r in chain})
        lots = {r["lot_size"] for r in chain}
        print(f"    options  : {len(opts)} rows, {len(exps)} expiries, next 3 = {exps[:3]}")
        print(f"    front    : {front} — {len(chain)} contracts, {len(strikes)} strikes")
        print(f"    strikes  : {strikes[0]} .. {strikes[-1]}  "
              f"(step {strikes[1] - strikes[0] if len(strikes) > 1 else '?'})")
        print(f"    lot_size : {lots}")
        s = chain[0]
        print(f"    sample   : token={s['instrument_token']} {s['tradingsymbol']} "
              f"strike={int(s['strike'])} {s['instrument_type']}")

    print("\n  If every index shows a SPOT token plus a populated front chain, the")
    print("  engine has everything it needs from Kite alone — no Fyers, no pandas.")


if __name__ == "__main__":
    main()
