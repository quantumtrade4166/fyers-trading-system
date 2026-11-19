"""
strategy_builder/practice_sync.py — copy practice days from the VPS to this PC (for the private practice copy).
=============================================================================================================

The VPS builds the days (tick recorder 15:50 task, Breeze packs). The PC's practice-only builder replays them
locally. This copies every day the PC does not have yet — or that the VPS rebuilt since (e.g. a morning-only
day rebuilt as the full day) — in THREE ssh/scp calls, never one per file (VPS SSH throttle).

The same file runs on both ends:
    PC   .venv\\Scripts\\python.exe -m live_trading_options.strategy_builder.practice_sync
    VPS  ... practice_sync --list                     -> JSON [[index, date, built], ...]
    VPS  ... practice_sync --zip NIFTY/2026-11-19,... -> one zip of those days on the Desktop
"""
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from live_trading_options.strategy_builder import practice_data as PD

VPS_ROOT = r"C:\trading\fyers_data_pipeline"
VPS_PY = VPS_ROOT + r"\.venv\Scripts\python.exe"
REMOTE_ZIP = r"C:\Users\Administrator\Desktop\sb_practice_sync.zip"


# ── VPS side ──
def serve_list():
    out = []
    for m in PD.PRACTICE_DIR.glob("*/*/meta.json"):
        try:
            out.append([m.parent.parent.name, m.parent.name, json.loads(m.read_text(encoding="utf-8")).get("built")])
        except Exception:
            pass
    print(json.dumps(out))


def serve_zip(spec: str):
    days = [x.split("/") for x in spec.split(",") if x]
    with zipfile.ZipFile(REMOTE_ZIP, "w", zipfile.ZIP_STORED) as z:          # parquet is already compressed
        for i, d in days:
            for f in (PD.PRACTICE_DIR / i / d).iterdir():
                z.write(f, f"{i}/{d}/{f.name}")
    print("zipped", len(days))


# ── PC side ──
def ssh(args: list, timeout=600) -> str:
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "vps", f"cd /d {VPS_ROOT} && {VPS_PY} -m "
                        f"live_trading_options.strategy_builder.practice_sync " + " ".join(args)],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return r.stdout


def sync():
    try:
        remote = json.loads(ssh(["--list"]).strip().splitlines()[-1])
    except Exception as e:
        print(f"could not reach the VPS ({e}) — replaying the days already on this PC")
        return
    local = {(p["index"], p["date"]): (PD.read_meta(p["index"], p["date"]) or {}).get("built") for p in PD.list_packs()}
    want = sorted([[i, d] for i, d, built in remote if local.get((i, d)) != built], key=lambda x: x[1], reverse=True)
    if not want:
        print(f"practice days up to date ({len(remote)} on the VPS)")
        return
    print(f"copying {len(want)} practice day(s) from the VPS: " + ", ".join(f"{i} {d}" for i, d in want[:8])
          + (" ..." if len(want) > 8 else ""))
    ssh(["--zip", ",".join(f"{i}/{d}" for i, d in want)])
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "sync.zip"
        r = subprocess.run(["scp", "-o", "ConnectTimeout=15", "vps:" + REMOTE_ZIP.replace("\\", "/"), str(dst)],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[:300])
        with zipfile.ZipFile(dst) as z:
            for i, d in want:                                   # replace each day whole (no stale files)
                folder = PD.pack_dir(i, d)
                if folder.exists():
                    for f in folder.iterdir():
                        f.unlink()
            z.extractall(PD.PRACTICE_DIR)
    print(f"done — {len(want)} day(s) copied")


if __name__ == "__main__":
    if "--list" in sys.argv:
        serve_list()
    elif "--zip" in sys.argv:
        serve_zip(sys.argv[sys.argv.index("--zip") + 1])
    else:
        sync()
