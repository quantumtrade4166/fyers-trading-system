"""tools/replay_equity.py — draw the curve of a session the engine did not trade.

Runs the SAME controller over the recorded chain archive for one day and writes
its per-minute MTM to data/results/<profile>_equity_replay.jsonl, every row tagged
"replay": true. The dashboard shows it only for a session with no live curve, and
labels it. Nothing is written to cycles.jsonl — a replay is not a result.

    python tools/replay_equity.py 2026-09-19 ist_day
"""
import sys, json, shutil, tempfile, datetime as dt
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backtest.run_backtest as rb
from backtest.replay import load_days
from core.sessions import SessionProfile
from live.controller import BTCController, RESULTS

day, prof = sys.argv[1], sys.argv[2]
d = dt.date.fromisoformat(day)
days = [(d - dt.timedelta(days=1)).isoformat(), day]      # full_cycle starts the evening before
df = load_days(days)
if df.empty:
    sys.exit(f"no archive for {days}")
base = {k: v for k, v in rb.PARAMS.items() if k != "sessions"}
cfg = dict(rb.PARAMS["sessions"][prof])
want = f"ist_day@{day}T09:30" if prof == "ist_day" else f"{prof}@{days[0]}T17:35"

sb = Path(tempfile.mkdtemp())
rb_cycles = None
# run_one drives the controller exactly as the live engine does; reuse its loop
ctrl_holder = {}
orig = BTCController.__init__
def grab(self, *a, **k):
    orig(self, *a, **k); ctrl_holder["c"] = self
BTCController.__init__ = grab
rb.run_one(prof, cfg, base, df)
BTCController.__init__ = orig
c = ctrl_holder["c"]
rows = [dict(r, replay=True) for r in c.equity if r.get("cycle") == want]
out = RESULTS / f"{prof}_equity_replay.jsonl"
keep = []
if out.exists():
    keep = [l for l in out.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("cycle") != want]
out.write_text("\n".join(keep + [json.dumps(r, default=str) for r in rows]) + "\n", encoding="utf-8")
fin = rows[-1]["mtm"] if rows else None
print(f"{want}: {len(rows)} replay samples, last mtm {fin}")
