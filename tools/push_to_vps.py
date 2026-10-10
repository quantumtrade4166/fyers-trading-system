import sys, os, subprocess, pathlib

sys.stdout.reconfigure(encoding="utf-8", errors="surrogatepass")

BASE = pathlib.Path(r"G:\fyers_data_pipeline")
REMOTE_REPO = r"C:\trading\fyers_data_pipeline"

files = [
    r"live_trading_options\strangle_strategy\live\kotak_controller.py",
    r"live_trading_options\strangle_strategy\live\kotak_executor.py",
    r"live_trading_options\strangle_strategy\live\kotak_auth.py",
    r"live_trading_options\strangle_strategy\live\kotak_rohit_controller.py",
    r"live_trading_options\strangle_strategy\live_tick_engine.py",
    r"deployment\main.py",
    r"deployment\static\index.html",
    r"live_trading_options\strangle_strategy\config\parameters.json",
]

# Build a self-contained Python script for the remote
parts = []
parts.append("import sys")
parts.append("sys.stdout.reconfigure(encoding='utf-8', errors='surrogatepass')")
parts.append(f"REPO = r'''{REMOTE_REPO}'''")
parts.append("import os")
parts.append("files = {}")

for rel in files:
    path = BASE / rel
    content = path.read_text(encoding="utf-8")
    # Escape for embedding in Python string
    escaped = content.replace("\\", "\\\\").replace("'''", "' + \"'''\" + '")
    parts.append(f"files[{repr(rel)}] = '''{escaped}'''")

parts.append("ok = 0")
parts.append("for rel, data in files.items():")
parts.append("    dst = os.path.join(REPO, rel.replace('/', os.sep))")
parts.append("    os.makedirs(os.path.dirname(dst), exist_ok=True)")
parts.append("    with open(dst, 'w', encoding='utf-8') as f:")
parts.append("        f.write(data)")
parts.append("    ok += 1")
parts.append(f"print(f'Written {{ok}}/{{len(files)}} files', flush=True)")

remote_script = "\n".join(parts)

# Pipe to SSH
proc = subprocess.run(
    ["ssh", "-o", "BatchMode=yes", "Administrator@144.79.166.103", "python -"],
    input=remote_script,
    capture_output=True,
    text=True,
    timeout=120,
)
print("STDOUT:", proc.stdout)
if proc.stderr:
    # Filter out known non-critical warnings
    stderr_lines = proc.stderr.strip().split("\n")
    for line in stderr_lines:
        if "post-quantum" not in line and "openssh.com/pq" not in line:
            print("STDERR:", line, file=sys.stderr)

print("Return code:", proc.returncode)
