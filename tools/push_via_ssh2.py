import sys, subprocess, pathlib, shlex
sys.stdout.reconfigure(encoding="utf-8", errors="surrogatepass")

base = pathlib.Path(r"G:\fyers_data_pipeline")
remote_repo = r"C:\Users\Administrator\Desktop\fyers_data_pipeline_git"

files = [
    r"live_trading_options\strangle_strategy\live\control_flags.py",
    r"deployment\static\index.html",
]

for rel in files:
    local = base / rel
    remote = remote_repo + "\\" + rel.replace("/", "\\")
    content = local.read_text(encoding="utf-8")

    remote_cmd = (
        "python -c "
        r"\"import sys,os; sys.stdout.reconfigure(encoding='utf-8',errors='surrogatepass'); "
        r"p=r'" + remote + r"'; "
        r"open(p,'w',encoding='utf-8').write(sys.stdin.read()); "
        r"print('OK '+str(os.path.getsize(p)))\""
    )

    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=5",
         "Administrator@144.79.166.103", remote_cmd],
        input=content.encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    out = (proc.stdout or b"").decode("utf-8", errors="surrogatepass").strip()
    err = (proc.stderr or b"").decode("utf-8", errors="surrogatepass").strip()[:200]
    print(f"{rel}: rc={proc.returncode} | {out} | {err[:120]}")
