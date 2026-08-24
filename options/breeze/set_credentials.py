"""One-shot credential setup for the Breeze headless login.

Prompts for the account login, writes it to the local deployment/.env, then
mirrors the BREEZE_* lines to the VPS .env over ssh.

Why a script instead of just editing the file: the password never appears on
screen, never lands in shell history, and never gets echoed to a terminal that
might be logged or recorded. getpass reads it straight off the tty.

Run once:
    .venv\\Scripts\\python.exe -m options.breeze.set_credentials

Then everything after it is unattended.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import getpass
import subprocess

from options.breeze.config import ENV_PATH, _load_env

VPS = "Administrator@144.79.166.103"
VPS_ENV = r"C:\Users\Administrator\Desktop\fyers_data_pipeline_git\deployment\.env"

KEYS = [
    ("BREEZE_API_KEY",     "Breeze API key",           False),
    ("BREEZE_API_SECRET",  "Breeze API secret",        True),
    ("BREEZE_USER_ID",     "ICICI Direct login ID",    False),
    ("BREEZE_PASSWORD",    "ICICI Direct password",    True),
    ("BREEZE_TOTP_SECRET", "Google Authenticator seed", True),
]


def write_local() -> dict:
    existing = _load_env()
    values = {}

    print(f"Writing to {ENV_PATH}")
    print("Press Enter to keep an existing value.\n")

    for key, label, secret in KEYS:
        have = existing.get(key, "")
        suffix = "  [already set]" if have else ""
        prompt = f"  {label}{suffix}: "
        entered = getpass.getpass(prompt) if secret else input(prompt).strip()
        values[key] = entered.strip() or have
        if not values[key]:
            print(f"    ⚠ {key} left empty — the login will fail without it.")

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    kept = [ln for ln in lines if not any(ln.strip().startswith(k + "=") for k, _, _ in KEYS)]
    while kept and not kept[-1].strip():
        kept.pop()

    kept.append("")
    kept += [f"{k}={values[k]}" for k, _, _ in KEYS if values[k]]
    ENV_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
    print(f"\n✅ Local .env updated ({sum(1 for k in values if values[k])} BREEZE keys).")
    return values


def push_to_vps(values: dict) -> None:
    """Mirror the BREEZE_* lines to the VPS without echoing them anywhere."""
    payload = "\n".join(f"{k}={values[k]}" for k, _, _ in KEYS if values[k])

    ps = (
        "$in=[Console]::In.ReadToEnd();"
        f"$p='{VPS_ENV}';"
        "$old = if (Test-Path $p) { Get-Content $p } else { @() };"
        "$keys = ($in -split \"`n\" | ForEach-Object { ($_ -split '=')[0] }) "
        "| Where-Object { $_ };"
        "$kept = $old | Where-Object { $k=($_ -split '=')[0]; $keys -notcontains $k };"
        "($kept + ($in -split \"`n\")) | Set-Content $p -Encoding utf8;"
        "Write-Output ('VPS .env now has ' + "
        "((Get-Content $p) -match '^BREEZE').Count + ' BREEZE keys')"
    )

    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=20", VPS, "powershell", "-NoProfile", "-Command", ps],
        input=payload, text=True, capture_output=True, timeout=90,
    )
    out = "\n".join(ln for ln in (r.stdout + r.stderr).splitlines()
                    if "post-quantum" not in ln and "store now" not in ln
                    and "WARNING" not in ln and "openssh.com" not in ln
                    and "upgraded" not in ln)
    print(out.strip() or "(no output)")
    if r.returncode != 0:
        print(f"⚠ ssh exited {r.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Set Breeze credentials, local + VPS")
    ap.add_argument("--local-only", action="store_true", help="skip the VPS copy")
    args = ap.parse_args()

    values = write_local()
    if not args.local_only:
        print("\nMirroring to the VPS...")
        push_to_vps(values)

    print("\nNext:  python -m options.breeze.auto_login --debug")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
