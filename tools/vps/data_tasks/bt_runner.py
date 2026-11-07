"""Run backtesting.options_st_directional.run_backtest with a hard per-request
timeout on Breeze history calls. The Breeze SDK uses requests with no timeout,
so one stalled read can hang a backtest forever (11 h on 6-Nov, 40+ min on 7-Nov).
A call that takes > 60 s raises TimeoutError("... timed out ..."), which the
downloader treats as transient and retries. The stuck thread is a daemon and is
simply abandoned.

    python tools\vps\data_tasks\bt_runner.py --start ... (same args as run_backtest)
"""
import runpy
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))   # repo root

from breeze_connect import BreezeConnect

TIMEOUT_S = 60
_orig = BreezeConnect.get_historical_data_v2


def _guarded(self, *a, **k):
    box = {}

    def run():
        try:
            box["r"] = _orig(self, *a, **k)
        except Exception as e:          # surfaced to the downloader as usual
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(TIMEOUT_S)
    if t.is_alive():
        print(f"    [timeout] Breeze call > {TIMEOUT_S}s abandoned, retrying", flush=True)
        raise TimeoutError(f"breeze request timed out after {TIMEOUT_S}s")
    if "e" in box:
        raise box["e"]
    return box["r"]


BreezeConnect.get_historical_data_v2 = _guarded
sys.argv = ["run_backtest"] + sys.argv[1:]
runpy.run_module("backtesting.options_st_directional.run_backtest", run_name="__main__")
