"""
tools/strangle_v2_export.py
===========================
Export the VWAP strangle V2 history into ONE compact JSON for a chart:
  - `curve`: day-by-day + cumulative realized P&L since inception (NIFTY+SENSEX, 1 lot)
  - `days` : every TRADED day (dte in {0,1}) with its combined-premium candle bars
             [time,o,h,l,c,volume,vwap] + the day's entry/exit events + realized P&L
Read-only. Writes data/pnl_history/vwap_v2_history.json.
"""

import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.chart_archive import ARCHIVE_DIR

INDICES = ("NIFTY", "SENSEX")


def main():
    files = {}
    for f in sorted(ARCHIVE_DIR.glob("*_V2.json")):
        try:
            j = json.loads(f.read_text())
        except Exception:
            continue
        idx, date = j.get("index"), j.get("date")
        if idx in INDICES and date:
            files[(date, idx)] = j

    # cumulative curve (all days; non-trade days contribute 0)
    bydate = {}
    for (date, idx), j in files.items():
        realized = (j.get("pnl") or {}).get("realized_pnl")
        bydate.setdefault(date, {"NIFTY": None, "SENSEX": None})[idx] = realized
    curve, cum = [], 0.0
    for date in sorted(bydate):
        n = bydate[date]["NIFTY"] or 0.0
        s = bydate[date]["SENSEX"] or 0.0
        cum = round(cum + n + s, 2)
        curve.append({"date": date, "nifty": bydate[date]["NIFTY"], "sensex": bydate[date]["SENSEX"],
                      "day_total": round(n + s, 2), "cumulative": cum})

    # per traded-day candle bars (dte in {0,1})
    days = []
    for (date, idx), j in sorted(files.items()):
        sel = j.get("selection") or {}
        if sel.get("dte") not in (0, 1):
            continue
        bars = [[c.get("time"), c.get("open"), c.get("high"), c.get("low"),
                 c.get("close"), c.get("volume"), c.get("vwap")]
                for c in (j.get("candles") or [])]
        days.append({"date": date, "index": idx, "dte": sel.get("dte"),
                     "ce": j.get("ce_symbol"), "pe": j.get("pe_symbol"),
                     "realized_pnl": (j.get("pnl") or {}).get("realized_pnl"),
                     "events": j.get("events") or [], "bars": bars})

    peak = mdd = 0.0
    for c in curve:
        peak = max(peak, c["cumulative"])
        mdd = min(mdd, c["cumulative"] - peak)
    summary = {"start": curve[0]["date"] if curve else None,
               "end": curve[-1]["date"] if curve else None,
               "trading_days": len(curve), "traded_day_charts": len(days),
               "final_cumulative_pnl_1lot": curve[-1]["cumulative"] if curve else 0.0,
               "peak": round(peak, 2), "max_drawdown": round(mdd, 2)}

    outdir = ROOT / "data" / "pnl_history"
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / "vwap_v2_history.json"
    out.write_text(json.dumps({"summary": summary, "curve": curve, "days": days}))
    print(json.dumps(summary, indent=2))
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(days)} traded-day charts)")


if __name__ == "__main__":
    main()
