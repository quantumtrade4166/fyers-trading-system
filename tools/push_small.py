import sys, subprocess, pathlib
sys.stdout.reconfigure(encoding="utf-8", errors="surrogatepass")

files = [
    (r"G:\fyers_data_pipeline\live_trading_options\strangle_strategy\live\control_flags.py",
     r"C:\Users\Administrator\Desktop\fyers_data_pipeline_git\live_trading_options\strangle_strategy\live\control_flags.py"),
    (r"G:\fyers_data_pipeline\deployment\static\index.html",
     r"C:\Users\Administrator\Desktop\fyers_data_pipeline_git\deployment\static\index.html"),
]

for local, remote in files:
    content = pathlib.Path(local).read_text(encoding="utf-8")
    # Write via SSH + python stdin
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=10",
         "Administrator@144.79.166.103",
         "python -c \"import sys; sys.stdout.reconfigure(encoding='utf-8',errors='surrogatepass'); "
         "f=open(r'" + remote + "','w',encoding='utf-8'); "
         "f.write(sys.stdin.read()); f.close(); print('OK')\""],
        input=content,
        capture_output=True,
        timeout=60,
    )
    status = "OK" if proc.returncode == 0 else f"FAIL({proc.returncode})"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()[:200]
    print(f"{local}: {status} | {out} | {err}")
