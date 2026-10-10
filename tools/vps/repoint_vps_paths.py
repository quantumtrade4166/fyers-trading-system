"""
repoint_vps_paths.py -- rewrite the old VPS paths in the repo to the new single root.

2026-10-11: the VPS was reinstalled. The code used to live on the Administrator Desktop
(C:\\Users\\Administrator\\Desktop\\fyers_data_pipeline_git) with launcher scripts loose on the
Desktop. The rebuilt VPS keeps everything under C:\\trading (one root, backed up whole), so:

    ...\\Desktop\\fyers_data_pipeline_git      -> C:\\trading\\fyers_data_pipeline
    ...\\Desktop\\restart_server.bat           -> <repo>\\deployment\\restart_server.bat
    ...\\Desktop\\start_dashboard.bat (etc.)   -> <repo>\\deployment\\vps_tasks\\...

Handles \\, \\\\ and / separators and UTF-16 task XML files. Run from the repo root:
    .venv\\Scripts\\python.exe tools\\vps\\repoint_vps_paths.py [--dry-run]
"""
import sys
import re
import subprocess
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
SKIP_SUFFIXES = {".md", ".log", ".ipynb", ".csv", ".parquet", ".png", ".jpg", ".pdf", ".docx", ".xlsx"}
SKIP_FILES = {"tools/vps/repoint_vps_paths.py"}

# launcher files that sat loose on the old Desktop -> where they live in the repo
DESKTOP_LAUNCHERS = {
    "restart_server.bat": "deployment/restart_server.bat",
    "start_dashboard.bat": "deployment/vps_tasks/start_dashboard.bat",
    "start_cloudflared.bat": "deployment/vps_tasks/start_cloudflared.bat",
    "fyers_auto_login.bat": "deployment/vps_tasks/fyers_auto_login.bat",
    "vps_heartbeat.ps1": "deployment/vps_tasks/vps_heartbeat.ps1",
}

SEP = r"(\\\\|\\|/)"
OLD_REPO = re.compile(r"C:" + SEP + r"Users\1Administrator\1Desktop\1fyers_data_pipeline_git", re.I)
OLD_LAUNCHER = re.compile(r"C:" + SEP + r"Users\1Administrator\1Desktop\1(" +
                          "|".join(re.escape(k) for k in DESKTOP_LAUNCHERS) + r")", re.I)


def new_repo(sep: str) -> str:
    return sep.join(["C:", "trading", "fyers_data_pipeline"])


def rewrite(text: str) -> str:
    text = OLD_LAUNCHER.sub(
        lambda m: new_repo(m.group(1)) + m.group(1) + DESKTOP_LAUNCHERS[m.group(2).lower()].replace("/", m.group(1)),
        text)
    return OLD_REPO.sub(lambda m: new_repo(m.group(1)), text)


def read(p: Path):
    raw = p.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return None, None


def main():
    dry = "--dry-run" in sys.argv
    files = subprocess.run(["git", "grep", "-l", "-i", "Administrator.Desktop"], cwd=ROOT,
                           capture_output=True, text=True).stdout.split()
    changed = 0
    for rel in files:
        p = ROOT / rel
        if rel in SKIP_FILES or p.suffix.lower() in SKIP_SUFFIXES or not p.is_file():
            continue
        text, enc = read(p)
        if text is None:
            continue
        new = rewrite(text)
        if new != text:
            changed += 1
            left = re.findall(r"C:[\\/]+Users[\\/]+Administrator[\\/]+Desktop[\\/]+[\w.\-]*", new, re.I)
            print(f"{'would fix' if dry else 'fixed'}: {rel}" + (f"   (still: {sorted(set(left))})" if left else ""))
            if not dry:
                if enc == "utf-16":
                    p.write_bytes(b"\xff\xfe" + new.encode("utf-16-le"))
                else:
                    p.write_bytes(new.encode("utf-8"))
    print(f"{changed} files {'to change' if dry else 'changed'}")


if __name__ == "__main__":
    main()
