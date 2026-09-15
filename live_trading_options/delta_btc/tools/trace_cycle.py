"""tools/trace_cycle.py — one profile's trades and 15-min windows, in order.

    .venv/Scripts/python.exe live_trading_options/delta_btc/tools/trace_cycle.py full_cycle 2026-09-14 20:40
"""
import sys
import json
import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
prof = sys.argv[1]
since = f"{sys.argv[2]} {sys.argv[3]}" if len(sys.argv) > 3 else ""

ev = []
for f in sorted(glob.glob(str(ROOT / "logs" / "*_btc_audit.log"))):
    for l in open(f, encoding="utf-8"):
        if l.startswith("{"):
            e = json.loads(l)
            if e.get("profile") == prof and e.get("ts", "") >= since:
                ev.append(e)

for e in ev:
    k, t = e["event"], e["ts"][5:16]
    if k == "window_status":
        c, p = e.get("ce") or {}, e.get("pe") or {}
        print(f"{t} WIN  spot {round(e['spot'] or 0)} | CE {c.get('strike')} {c.get('mark')} sl {c.get('sl')}"
              f" | PE {p.get('strike')} {p.get('mark')} sl {p.get('sl')} | mtm {round(e['mtm'] or 0)} | {e.get('action','')[:26]}")
    elif k in ("leg_open", "leg_closed", "stop_hit", "adjust_triggered", "adjust_held", "reentry",
               "sl_resynced", "cycle_open", "cycle_closed", "entry_start"):
        keep = ("side", "strike", "entry", "exit", "fill", "trigger", "mark", "sl", "replace",
                "from_strike", "to_strike", "new_premium", "premium", "below", "reason", "before", "after")
        print(f"{t} {k.upper()} " + " ".join(f"{x}={e[x]}" for x in keep if e.get(x) is not None))
