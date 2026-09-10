"""
deployment/check_dashboard_js.py — refuse to ship a dashboard that will not parse.
=================================================================================

WHY THIS EXISTS

On 2026-09-11 the committed `static/index.html` was found to contain a JavaScript
SYNTAX ERROR: the closing brace of `btcStopPoll()` had been cut off by a script
that spliced the file together. The whole inline <script> then fails to parse, so
EVERY tab dies — StatArb, DualMom, Strangle, Terminal, all of them — not just the
one being edited.

It survived several deploys undetected for one reason: the VPS was receiving the
working-tree file by scp, which was intact, while the file in git was not. Anyone
doing a plain `git pull` would have got a dead dashboard, and nothing in the
pipeline would have said so.

A brace is not something to check by eye in a 4,000-line file, and counting braces
in Python is unreliable because template literals and strings contain them. So the
real parser is used.

Run it before committing or deploying index.html:

    .venv/Scripts/python.exe deployment/check_dashboard_js.py

Exit code 0 means the page will parse; 1 means it will not. Needs node on PATH; if
node is missing it says so and exits 2 rather than passing silently, because "the
check could not run" must never look like "the check passed".
"""

import re
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

HTML = Path(__file__).resolve().parent / "static" / "index.html"


def main() -> int:
    if not HTML.exists():
        print(f"  MISSING: {HTML}")
        return 1

    node = shutil.which("node")
    if not node:
        print("  node not found on PATH — cannot verify the dashboard's JavaScript.")
        print("  This is NOT a pass. Install node or check the file another way.")
        return 2

    s = HTML.read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", s, re.S)
    if not blocks:
        print("  no inline <script> block found — is this the right file?")
        return 1

    bad = 0
    for i, js in enumerate(blocks, 1):
        if not js.strip():
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(js)
            tmp = f.name
        try:
            r = subprocess.run([node, "--check", tmp],
                               capture_output=True, text=True)
        finally:
            Path(tmp).unlink(missing_ok=True)
        if r.returncode == 0:
            print(f"  block {i}: OK ({len(js):,} chars)")
        else:
            bad += 1
            print(f"  block {i}: SYNTAX ERROR ({len(js):,} chars)")
            # node points at the line inside the block; show it with context so the
            # damage is obvious rather than having to be hunted
            err = (r.stderr or "").strip().splitlines()
            for line in err[:6]:
                print(f"      {line}")
            m = re.search(r":(\d+)\s*$", err[0]) if err else None
            if m:
                n = int(m.group(1))
                lines = js.splitlines()
                for ln in range(max(0, n - 4), min(len(lines), n + 1)):
                    print(f"      {ln + 1:>5} | {lines[ln]}")

    if bad:
        print(f"\n  {bad} block(s) will NOT parse. The dashboard would be dead in the "
              f"browser — every tab, not just the one you edited. DO NOT DEPLOY.")
        return 1
    print("\n  dashboard JavaScript parses cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
