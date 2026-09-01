"""
tools/import_kotak_creds.py — move Kotak creds from a local text file into deployment/.env.
===========================================================================================

Run this ON THE VPS, by you — the credential VALUES never leave this machine and are never
printed. It reads your credentials text file, picks out the 4 Kotak keys, and writes them
into deployment/.env (replacing any existing KOTAK_* lines). It prints ONLY success + the
key names, never the values.

    python tools/import_kotak_creds.py
    python tools/import_kotak_creds.py "C:\\path\\to\\your creds.txt"     # custom path

Accepts any of these line formats in the txt file (per key, case-insensitive labels):
    KOTAK_CONSUMER_KEY=xxxx        |  Consumer Key: xxxx        |  consumer key   xxxx
"""

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_SRC = Path(r"C:\Users\Administrator\Desktop\kotak api credentials.txt")
ENV = Path(__file__).resolve().parents[3] / "deployment" / ".env"   # G:\...\deployment\.env

# key -> keywords that identify its line (checked lowercased). Order matters: the more
# specific keys (UCC) are matched before the looser ones (MOBILE) so a "UCC" line isn't
# swallowed by the mobile matcher.
KEYS = {
    "KOTAK_CONSUMER_KEY": ("consumer", "api key", "apikey", "consumer_key", "consumerkey"),
    "KOTAK_UCC":          ("ucc", "client code", "unique client", "client_code", "clientcode"),
    "KOTAK_MOBILE":       ("mobile", "phone"),
    "KOTAK_MPIN":         ("mpin", "m-pin", "pin"),
    "KOTAK_TOTP_SECRET":  ("totp", "secret", "seed"),
}


def _extract(lines: list) -> dict:
    found = {}
    # pass 1 — explicit KOTAK_KEY=value / KOTAK_KEY: value
    for ln in lines:
        m = re.match(r"\s*(KOTAK_[A-Z_]+)\s*[:=]\s*(\S.*)", ln)
        if m and m.group(1) in KEYS:
            found[m.group(1)] = m.group(2).strip()
    # pass 2 — labelled lines by keyword (skip already found)
    for key, kws in KEYS.items():
        if key in found:
            continue
        for ln in lines:
            low = ln.lower()
            if any(kw in low for kw in kws):
                parts = re.split(r"[:=]", ln, 1)
                val = parts[1].strip() if len(parts) > 1 else ln.split()[-1]
                if val and not val.upper().startswith("KOTAK_"):
                    found[key] = val
                    break
    return found


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    if not src.exists():
        print(f"ERROR: credentials file not found: {src}")
        print("Pass the path:  python tools/import_kotak_creds.py \"C:\\path\\to\\creds.txt\"")
        sys.exit(1)

    lines = [ln.rstrip() for ln in src.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    found = _extract(lines)
    missing = [k for k in KEYS if k not in found]
    if missing:
        print("COULD NOT FIND:", ", ".join(missing))
        print("Fix the labels in the txt file (e.g. 'KOTAK_MPIN=1234') and re-run.")
        sys.exit(1)

    ENV.parent.mkdir(parents=True, exist_ok=True)
    existing = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    kept = [ln for ln in existing if not re.match(r"\s*KOTAK_[A-Z_]+\s*=", ln)]
    kept += [f"{k}={found[k]}" for k in KEYS]
    ENV.write_text("\n".join(kept) + "\n", encoding="utf-8")
    print(f"OK — wrote {len(KEYS)} keys to {ENV}: " + ", ".join(KEYS) + "  (values hidden)")


if __name__ == "__main__":
    main()
