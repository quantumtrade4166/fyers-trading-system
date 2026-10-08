"""Deploy changed files to VPS via SSH stdin. Used after SSH cooldown."""
import subprocess
import pathlib

VPS = "Administrator@144.79.166.103"
KEY = str(pathlib.Path.home() / ".ssh" / "id_rsa")

REMOTE_ROOT = r"C:\Users\Administrator\Desktop\fyers_data_pipeline_git"

FILES = [
    (
        r"G:\fyers_data_pipeline\live_trading_options\strangle_strategy\live\control_flags.py",
        REMOTE_ROOT + r"\live_trading_options\strangle_strategy\live\control_flags.py",
    ),
    (
        r"G:\fyers_data_pipeline\deployment\static\index.html",
        REMOTE_ROOT + r"\deployment\static\index.html",
    ),
]

# Build a python -c command that reads its stdin and writes it to a file
# We construct it carefully with double quotes so Windows path backslashes survive
def make_writer(remote_path: str) -> str:
    # Use forward slashes inside the path to dodge escape hell; Python on Windows handles them fine
    rp = remote_path.replace("\\", "/")
    return (
        'python -c "'
        "import sys;"
        "f=open(r'" + rp + "','w',encoding='utf-8');"
        "f.write(sys.stdin.read());"
        "f.close();"
        "print('DONE')"
        '"'
    )

for local, remote in FILES:
    content = pathlib.Path(local).read_text(encoding="utf-8").encode("utf-8")
    cmd = [
        "ssh", "-i", KEY,
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=10",
        "-o", "StrictHostKeyChecking=no",
        VPS,
        make_writer(remote),
    ]
    proc = subprocess.run(cmd, input=content, capture_output=True, timeout=60)
    status = "OK" if proc.returncode == 0 else f"FAIL({proc.returncode})"
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()[:200]
    err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()[:300]
    print(f"[{local}]")
    print(f"  -> {status} | out={out!r} | err={err!r}")
