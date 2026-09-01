"""
live/kotak_probe.py — INSPECT the Kotak Neo v2 API shapes. Places NOTHING.
=========================================================================

Run this ONCE after the SDK is installed and deployment/.env is filled. It logs in and
dumps the real response formats we need to build the executor correctly — the symbol
master (so we resolve the exact option trading_symbol), the order report, and positions.
Zero orders, read-only. The output tells us the exact field names to parse.

    python live/kotak_probe.py            # from .../strangle_strategy
"""

import sys
import json
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live.kotak_auth import login


def _head(obj, n=3):
    """A compact, safe preview of whatever the SDK returns (dict / list / str / df)."""
    try:
        if isinstance(obj, dict):
            return {"type": "dict", "keys": list(obj.keys())[:20],
                    "sample": {k: obj[k] for k in list(obj.keys())[:3]}}
        if isinstance(obj, list):
            return {"type": "list", "len": len(obj), "first": obj[:n]}
        if hasattr(obj, "head") and hasattr(obj, "columns"):        # pandas DataFrame
            return {"type": "DataFrame", "columns": list(obj.columns),
                    "rows": obj.head(n).to_dict("records")}
        return {"type": type(obj).__name__, "repr": str(obj)[:800]}
    except Exception as e:
        return {"preview_error": f"{type(e).__name__}: {e}"}


def _try(label, fn):
    print(f"\n===== {label} =====", flush=True)
    try:
        print(json.dumps(_head(fn()), indent=2, default=str)[:3000], flush=True)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}", flush=True)


def main():
    client = login()
    print("\nLOGIN OK — inspecting API shapes (no orders placed).", flush=True)

    # 1) symbol master per segment — the source of the exact option trading_symbol
    _try("scrip_master(nse_fo)  [NIFTY options]",
         lambda: client.scrip_master(exchange_segment="nse_fo"))
    _try("scrip_master(bse_fo)  [SENSEX options]",
         lambda: client.scrip_master(exchange_segment="bse_fo"))

    # 2) search_scrip — the convenience resolver (params + return shape)
    _try("search_scrip(nse_fo NIFTY CE)",
         lambda: client.search_scrip(exchange_segment="nse_fo", symbol="NIFTY",
                                     expiry="", option_type="CE", strike_price=""))

    # 3) order report + positions — the reconciliation / own-book shapes
    _try("order_report()", lambda: client.order_report())
    _try("positions()", lambda: client.positions())

    # 4) funds/limits — to sanity-check margin later
    _try("limits()", lambda: client.limits())

    print("\nDONE. Paste this whole output back so I can build the executor exactly.", flush=True)


if __name__ == "__main__":
    main()
