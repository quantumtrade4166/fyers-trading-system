"""Wire the Nifty ST Directional paper engine into the dashboard. Idempotent.

Edits the files IN PLACE on whichever machine runs it, so it works on the VPS
copies even where they differ from local (anchors, not whole-file copies):
  deployment/main.py         include the /api/stdir router
  deployment/scheduler.py    _ensure_stdir_running() watchdog + its call
  deployment/static/index.html   "Nifty ST Dir" tab (iframe -> /static/stdir.html)

    .venv\\Scripts\\python.exe tools\\vps\\patch_stdir.py [--check]
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHECK = "--check" in sys.argv

MAIN_ANCHOR = '''    print(f"[ndp] router NOT loaded: {type(_e).__name__}: {_e}", flush=True)
'''
MAIN_ADD = '''
# Nifty ST Directional (PAPER). Separate engine process publishes files; read-only router.
try:
    from deployment.stdir_api import router as _stdir_router
    app.include_router(_stdir_router)
except Exception as _e:                                    # pragma: no cover
    print(f"[stdir] router NOT loaded: {type(_e).__name__}: {_e}", flush=True)
'''

SCHED_CALL_ANCHOR = '''        print(f"  [scheduler] ensure_ndp failed: {e}")
'''
SCHED_CALL_ADD = '''    # Nifty ST Directional (paper) — independent of every engine above
    try:
        _ensure_stdir_running()
    except Exception as e:
        print(f"  [scheduler] ensure_stdir failed: {e}")
'''
SCHED_FUNC_ANCHOR = "def _start_feed():"
SCHED_FUNC_ADD = '''_STDIR_PS_FILTER = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' "
                    "-and $_.CommandLine -like '*nifty_st_directional*engine*' }")


def _ensure_stdir_running():
    """Keep the Nifty ST Directional paper engine alive 09:10-15:20 IST. Staleness
    from TICK.json, which its writer thread rewrites every second."""
    import datetime, subprocess
    from pathlib import Path
    import pytz
    now = datetime.datetime.now(pytz.timezone("Asia/Kolkata"))
    if not (now.weekday() < 5 and (9, 10) <= (now.hour, now.minute) <= (15, 20)):
        return
    tick = (Path(__file__).parent.parent / "live_trading_options" / "nifty_st_directional"
            / "data" / "live_state" / "TICK.json")
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"({_STDIR_PS_FILTER} | Measure-Object).Count"],
                       capture_output=True, text=True, timeout=20)
    running = (r.stdout.strip() or "0") != "0"
    gap = (datetime.datetime.now().timestamp() - tick.stat().st_mtime) if tick.exists() else None
    stalled = running and now.time() >= datetime.time(9, 14) and (gap is None or gap > 120)
    if (not running) or stalled:
        if stalled:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"{_STDIR_PS_FILTER} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
                           capture_output=True, timeout=20)
        subprocess.run(["schtasks", "/Run", "/TN", "NiftyStDirEngine"], capture_output=True, timeout=20)
        print(f"  [scheduler] STDIR {'restart' if stalled else 'start'} (running={running}, tick_gap={gap})")


'''

HTML_BTN_ANCHOR = '''    <button class="tab-btn" id="tab-ndp" onclick="switchTab('ndp')">
      📐 Nifty Directional Pivot
    </button>
'''
HTML_BTN_ADD = '''    <button class="tab-btn" id="tab-stdir" onclick="switchTab('stdir')">
      🧭 Nifty ST Directional
    </button>
'''
HTML_PAGE_ANCHOR = '''  <div id="page-ndp" style="display:none">'''
HTML_PAGE_ADD = '''  <div id="page-stdir" style="display:none">
    <iframe id="stdir-frame" title="Nifty ST Directional"
            style="width:100%;height:calc(100vh - 110px);min-height:900px;border:0;display:block"></iframe>
  </div>

'''
HTML_SWITCH_ANCHOR = '''  if (tab === 'ndp') { ndpInit(); ndpStartPoll(); } else { ndpStopPoll(); }
'''
HTML_SWITCH_ADD = '''  if (tab === 'stdir') { const f = document.getElementById('stdir-frame'); if (f && !f.src) f.src = '/static/stdir.html'; }
'''
HTML_LIST_OLD = "'btcarb', 'ndp', 'terminal'].forEach"
HTML_LIST_NEW = "'btcarb', 'ndp', 'stdir', 'terminal'].forEach"


def patch(path, edits):
    p = REPO / path
    s = p.read_text(encoding="utf-8")
    changed = False
    for marker, anchor, add, where in edits:
        if marker in s:
            print(f"  {path}: already has {marker[:40]!r}")
            continue
        if s.count(anchor) != 1:
            raise SystemExit(f"  {path}: anchor not found exactly once: {anchor[:60]!r}")
        if where == "after":
            s = s.replace(anchor, anchor + add)
        elif where == "before":
            s = s.replace(anchor, add + anchor)
        else:                                   # replace
            s = s.replace(anchor, add)
        changed = True
        print(f"  {path}: + {marker[:50]!r}")
    if changed and not CHECK:
        p.write_text(s, encoding="utf-8")
    return changed


def main():
    patch("deployment/main.py", [("deployment.stdir_api", MAIN_ANCHOR, MAIN_ADD, "after")])
    patch("deployment/scheduler.py", [
        ("_ensure_stdir_running()\n    except", SCHED_CALL_ANCHOR, SCHED_CALL_ADD, "after"),
        ("def _ensure_stdir_running", SCHED_FUNC_ANCHOR, SCHED_FUNC_ADD, "before"),
    ])
    patch("deployment/static/index.html", [
        ('id="tab-stdir"', HTML_BTN_ANCHOR, HTML_BTN_ADD, "after"),
        ('id="page-stdir"', HTML_PAGE_ANCHOR, HTML_PAGE_ADD, "before"),
        ("tab === 'stdir'", HTML_SWITCH_ANCHOR, HTML_SWITCH_ADD, "after"),
        (HTML_LIST_NEW, HTML_LIST_OLD, HTML_LIST_NEW, "replace"),
    ])
    patch("tools/vps/install_vps_tasks.ps1", [(
        "'NiftyStDirEngine'",
        """Add-Task 'NiftyPivotEngine'    "$repo\\live_trading_options\\nifty_pivot\\run_pivot_engine.bat" $null @(Daily '09:10')\n""",
        """Add-Task 'NiftyStDirEngine'    "$repo\\live_trading_options\\nifty_st_directional\\run_stdir_engine.bat" $null @(Daily '09:10')\n""",
        "after")])
    print("check only - nothing written" if CHECK else "done")


if __name__ == "__main__":
    main()
