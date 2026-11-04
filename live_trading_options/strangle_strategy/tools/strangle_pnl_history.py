"""
tools/strangle_pnl_history.py
=============================
Scan the V2 candle archives (data/chart_history/{date}_{index}_V2.json) for NIFTY and
SENSEX, pull each day's realized P&L (the archive's `pnl.realized_pnl` — the strategy's
own entry/exit rules simulated on that day's REAL combined-premium candles, 1 lot), and
build the day-by-day + cumulative-since-inception series.

1-lot, strategy-signal P&L (consistent across every day, incl. days the live engine was
down) — NOT the actual multi-lot broker-fill P&L. Scale by lots to get rupees.

Outputs data/pnl_history/vwap_pnl_curve.{json,csv}. Read-only.
"""

import sys
import json
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.chart_archive import ARCHIVE_DIR

INDICES = ("NIFTY", "SENSEX")


def collect():
    recs = []
    for f in sorted(ARCHIVE_DIR.glob("*_V2.json")):
        try:
            j = json.loads(f.read_text())
        except Exception:
            continue
        idx, date = j.get("index"), j.get("date")
        if idx not in INDICES or not date:
            continue
        pnl = j.get("pnl") or {}
        sel = j.get("selection") or {}
        recs.append({"date": date, "index": idx,
                     "realized_pnl": pnl.get("realized_pnl"),
                     "realized_points": pnl.get("realized_points"),
                     "n_candles": j.get("n_candles"),
                     "ntrades": len(pnl.get("trades") or []),
                     "tradeable": sel.get("tradeable"),
                     "dte": sel.get("dte"),
                     "source": j.get("source", "live_engine")})
    return recs


def build():
    recs = collect()
    bydate = {}
    for r in recs:
        d = bydate.setdefault(r["date"], {"date": r["date"], "NIFTY": None, "SENSEX": None})
        d[r["index"]] = r["realized_pnl"]
    curve, cum = [], 0.0
    for k in sorted(bydate):
        d = bydate[k]
        n = d["NIFTY"] or 0.0
        s = d["SENSEX"] or 0.0
        day_total = round(n + s, 2)
        cum = round(cum + day_total, 2)
        curve.append({"date": k, "nifty_pnl": d["NIFTY"], "sensex_pnl": d["SENSEX"],
                      "day_total": day_total, "cumulative": cum})
    return recs, curve


def main():
    recs, curve = build()
    summary = {"v2_files": len(recs), "trading_days": len(curve),
               "start": curve[0]["date"] if curve else None,
               "end": curve[-1]["date"] if curve else None,
               "final_cumulative_pnl_1lot": curve[-1]["cumulative"] if curve else 0.0,
               "nifty_days": sum(1 for r in recs if r["index"] == "NIFTY"),
               "sensex_days": sum(1 for r in recs if r["index"] == "SENSEX")}
    outdir = ROOT / "data" / "pnl_history"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "vwap_pnl_curve.json").write_text(
        json.dumps({"summary": summary, "curve": curve, "records": recs}, indent=2))
    with open(outdir / "vwap_pnl_curve.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "nifty_pnl", "sensex_pnl", "day_total", "cumulative"])
        for c in curve:
            w.writerow([c["date"], c["nifty_pnl"], c["sensex_pnl"], c["day_total"], c["cumulative"]])
    print(json.dumps(summary, indent=2))
    if curve:
        print("first:", curve[0])
        print("last :", curve[-1])
    print(f"wrote {outdir}")


if __name__ == "__main__":
    main()
