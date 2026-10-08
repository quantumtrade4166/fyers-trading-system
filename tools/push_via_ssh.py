import sys, subprocess, pathlib
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

    # Build one-liner that reads stdin via python
    remote_cmd = (
        "python -c "
        "'import sys; "
        "sys.stdout.reconfigure(encoding=\"utf-8\",errors=\"surrogatepass\"); "
        "p=r\"'" + remote + "'\"; "
        "open(p,\"w\",encoding=\"utf-8\").write(sys.stdin.read()); "
        "print(\"OK\",__import__(\"os\").path.getsize(p))'"
    )

    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=5",
         "Administrator@144.79.166.103", remote_cmd],
        input=content,
        capture_output=True,
        timeout=120,
    )
    out = (proc.stdout or b"").decode("utf-8", errors="surrogatepass").strip()
    err_lines = (proc.stderr or b"").decode("utf-8", errors="surrogatepass").strip().split("\n")
    err = " ".join(l for l in err_lines if "post-quantum" not in l and "openssh.com/pq" not in l and "vulnerable" not in l)
    print(f"{rel}: rc={proc.returncode} | {out} | {err[:120]}")
