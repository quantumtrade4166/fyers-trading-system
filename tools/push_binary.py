import sys, subprocess, pathlib
sys.stdout.reconfigure(encoding="utf-8", errors="surrogatepass")

base = pathlib.Path(r"G:\fyers_data_pipeline")
remote_repo = r"C:\trading\fyers_data_pipeline"

files = [
    r"live_trading_options\strangle_strategy\live\control_flags.py",
    r"deployment\static\index.html",
]

for rel in files:
    local = base / rel
    remote = remote_repo + "\\" + rel.replace("/", "\\")
    data = local.read_bytes()

    # Use a write_via_stdin.py script on VPS
    remote_script = (
        "import sys; sys.stdout.reconfigure(encoding='utf-8',errors='surrogatepass');"
        " open(sys.argv[1],'wb').write(sys.stdin.buffer.read());"
        " print(f'Wrote {len(sys.stdin.buffer.read())} bytes to {sys.argv[1]}')"
    )
    # Can't read stdin twice, so use file-based approach
    remote_script2 = (
        "import sys, os; sys.stdout.reconfigure(encoding='utf-8',errors='surrogatepass');"
        " f=open(r'" + remote + "','wb');"
        " f.write(sys.stdin.buffer.read()); f.close();"
        " print('OK ' + str(os.path.getsize(r'" + remote + "')))"
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=5",
         "Administrator@103.49.131.58",
         "python -c \"" + remote_script2.replace('"', '\\\\\"') + "\""],
        input=data,
        capture_output=True,
        timeout=120,
    )
    out = (proc.stdout or b"").decode("utf-8", errors="surrogatepass").strip()
    err = (proc.stderr or b"").decode("utf-8", errors="surrogatepass").strip()[:200]
    print(f"{rel}: rc={proc.returncode} | {out} | {err}")
