"""
Import the DualMom Kotak credentials from a plain-text file into deployment/.env.

WHY THIS EXISTS
    The credentials are live broker secrets. This script parses the text file and
    writes the KOTAK_DM_* entries itself, so the values never have to be read out,
    pasted into a chat, or echoed to a terminal. It prints field NAMES and lengths
    only -- never a value.

USAGE
    # 1. preview what it found (writes nothing)
    .venv\\Scripts\\python.exe -m deployment.dualmom_live.import_creds "<path to txt>"

    # 2. if the preview looks right, write it
    .venv\\Scripts\\python.exe -m deployment.dualmom_live.import_creds "<path to txt>" --write

SAFETY
    * Refuses to write any value identical to the strangle's KOTAK_* set -- that
      would point DualMom at the strangle's account and steal its session mid-day.
    * Backs up deployment/.env before touching it.
    * Validates the TOTP secret by generating a code (never prints the code).
"""

import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
ENV = ROOT / "deployment" / ".env"

NEEDED = ("CONSUMER_KEY", "MOBILE", "UCC", "MPIN", "TOTP_SECRET")

# every spelling these files realistically use, normalised to our five names
SYNONYMS = {
    "CONSUMER_KEY": ("consumer key", "consumerkey", "consumer_key", "api key",
                     "apikey", "api_key", "app key", "appkey", "key"),
    "CONSUMER_SECRET": ("consumer secret", "consumersecret", "consumer_secret",
                        "api secret", "apisecret", "app secret", "secret key"),
    "MOBILE": ("mobile", "mobile number", "mobile no", "phone", "phone number",
               "mobileno", "mobile_number", "contact"),
    "UCC": ("ucc", "client code", "client id", "clientcode", "clientid",
            "user id", "userid", "trading id"),
    "MPIN": ("mpin", "m pin", "m-pin", "pin"),
    "TOTP_SECRET": ("totp", "totp secret", "totp_secret", "totpsecret",
                    "authenticator", "auth secret", "2fa", "2fa secret",
                    "totp key", "secret"),
    "PASSWORD": ("password", "pwd", "login password"),
}


# Shape validators. The labels in these files are not always unambiguous --
# "KOTAK_MOBILE (or UCC)" names TWO fields -- so when a label matches more than
# one, the VALUE decides. Without this the phone number lands in UCC and the
# login fails with a useless broker error.
VALID = {
    "CONSUMER_KEY":   lambda v: len(v) >= 12 and re.fullmatch(r"[A-Za-z0-9_\-]+", v) is not None,
    "CONSUMER_SECRET": lambda v: len(v) >= 8,
    "MOBILE":         lambda v: re.fullmatch(r"\+?\d{10,13}", v.replace(" ", "")) is not None,
    "UCC":            lambda v: re.fullmatch(r"[A-Za-z0-9]{3,12}", v) is not None,
    "MPIN":           lambda v: re.fullmatch(r"\d{4,8}", v) is not None,
    "TOTP_SECRET":    lambda v: re.fullmatch(r"[A-Za-z2-7= ]{16,}", v) is not None,
    "PASSWORD":       lambda v: True,
}


def fits(field: str, value: str) -> bool:
    try:
        return bool(VALID.get(field, lambda v: True)(value))
    except Exception:
        return False


def normalise(k: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", k.lower()).strip()


def classify(key: str, value: str = ""):
    """Map a label onto one of our field names, using the VALUE to break ties.

    'consumer secret' must not match the bare 'secret' belonging to TOTP_SECRET,
    so longer label matches win. But when one label names two fields -- the real
    file has "KOTAK_MOBILE (or UCC)" -- the label cannot decide, and only the
    value's shape can.
    """
    n = normalise(key)
    cands = {}
    for field, alts in SYNONYMS.items():
        for a in alts:
            hit = (n == a or n.startswith(a + " ") or n.endswith(" " + a)
                   or f" {a} " in f" {n} ")
            if hit:
                cands[field] = max(cands.get(field, 0), len(a))
    if not cands:                                   # looser containment pass
        for field, alts in SYNONYMS.items():
            for a in alts:
                if a in n:
                    cands[field] = max(cands.get(field, 0), len(a))
    if not cands:
        return None
    if len(cands) > 1 and value:
        ok = {f: L for f, L in cands.items() if fits(f, value)}
        if ok:
            cands = ok
    return max(cands.items(), key=lambda kv: kv[1])[0]


def parse(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    found, unmapped = {}, []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([^:=]{1,60})[:=]\s*(.+)$", s)
        if m:
            label, value = m.group(1).strip(), m.group(2).strip()
        else:
            # Fallback: whitespace-separated, e.g.
            #     KOTAK_CONSUMER_KEY      <value>
            #     KOTAK_MOBILE (or UCC)   <value>
            # No credential value contains a space, so the LAST token is the
            # value and everything before it is the label.
            toks = s.split()
            if len(toks) < 2:
                continue
            label, value = " ".join(toks[:-1]), toks[-1]
        label = label.rstrip("=: ").strip()
        value = value.strip().strip('"\'')
        if not value:
            continue
        field = classify(label, value)
        if field is None:
            unmapped.append(label)
        elif field not in found:
            found[field] = value
        elif fits(field, value) and not fits(field, found[field]):
            found[field] = value                  # a better-shaped value wins
    return found, unmapped


def env_values() -> dict:
    if not ENV.exists():
        return {}
    out = {}
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*([A-Z_][A-Z_0-9]*)\s*=\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def mask(v: str) -> str:
    return f"<{len(v)} chars>"


def shape(path: Path):
    """Print the file's STRUCTURE without leaking values.

    Keeps short pure-alphabetic words (labels like 'Consumer Key', 'MPIN') and
    masks anything that looks like a secret: tokens containing digits, or longer
    than 12 characters. Enough to fix the label mapping, useless to an attacker.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    print(f"\nSHAPE of {path.name} — {len(raw.splitlines())} lines, "
          f"values masked\n" + "=" * 66)
    for n, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        out = []
        for tok in re.split(r"(\s+)", line.rstrip()):
            if not tok.strip():
                out.append(tok)
            elif re.fullmatch(r"[A-Za-z:=\-_.()#/]{1,12}", tok):
                out.append(tok)                       # a label word — safe
            else:
                out.append(f"<{len(tok)}>")           # anything else — masked
        print(f"  {n:>3} | {''.join(out)}")
    print("=" * 66)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    write = "--write" in sys.argv
    if not args:
        print(__doc__)
        sys.exit(2)
    src = Path(args[0])
    if not src.exists():
        print(f"ERROR: no such file: {src}")
        sys.exit(1)

    if "--shape" in sys.argv:
        shape(src)
        return

    found, unmapped = parse(src)
    print(f"\nSource: {src.name}")
    print("=" * 66)

    missing = [f for f in NEEDED if f not in found]
    for f in NEEDED:
        if f in found and not fits(f, found[f]):
            print(f"  BAD SHAPE KOTAK_DM_{f} — value does not look like a {f}")
    for f in NEEDED:
        state = "OK " if f in found else "MISSING"
        print(f"  {state:<8} KOTAK_DM_{f:<14} {mask(found[f]) if f in found else '--'}")
    for extra in ("CONSUMER_SECRET", "PASSWORD"):
        if extra in found:
            print(f"  (spare)  {extra:<24} {mask(found[extra])}  not needed by Neo v2")
    if unmapped:
        print(f"\n  unrecognised labels (ignored): {', '.join(unmapped[:8])}")

    if missing:
        print(f"\nCANNOT PROCEED — missing: {', '.join('KOTAK_DM_' + m for m in missing)}")
        print("Add them to the text file, or pass them another way.")
        sys.exit(1)

    # --- validate TOTP without ever printing the code ---
    try:
        import pyotp
        code = pyotp.TOTP(found["TOTP_SECRET"].replace(" ", "")).now()
        assert code.isdigit() and len(code) == 6
        print("\n  TOTP secret generates a valid 6-digit code  OK")
    except Exception as e:
        print(f"\n  TOTP secret FAILED to generate a code: {type(e).__name__}: {e}")
        print("  (is it the base32 secret, not the 6-digit code shown in the app?)")
        sys.exit(1)

    # --- shape checks ---
    warn = []
    if not re.fullmatch(r"\+?\d{10,13}", found["MOBILE"].replace(" ", "")):
        warn.append("MOBILE does not look like a phone number")
    if not found["MPIN"].isdigit():
        warn.append("MPIN is not all digits")
    for w in warn:
        print(f"  WARNING: {w}")

    # --- collision guard against the strangle account ---
    #
    # Two sources, because .env alone is not enough: on the LOCAL machine the
    # strangle's KOTAK_* are absent (they live on the VPS), so the .env check
    # silently passes and protects nothing. --compare points at the strangle's
    # own credentials FILE so the check works wherever this is run.
    cur = env_values()
    clashes = [f for f in NEEDED
               if cur.get("KOTAK_" + f) and cur["KOTAK_" + f] == found[f]]
    cmp_path = None
    for i, a in enumerate(sys.argv):
        if a == "--compare" and i + 1 < len(sys.argv):
            cmp_path = Path(sys.argv[i + 1])
    if cmp_path:
        if not cmp_path.exists():
            print(f"\nERROR: --compare file not found: {cmp_path}")
            sys.exit(1)
        other, _ = parse(cmp_path)
        shared = sorted(f for f in NEEDED
                        if f in other and other[f] == found[f])
        if shared:
            clashes = sorted(set(clashes) | set(shared))
        print(f"  compared against {cmp_path.name}: "
              f"{'SHARED VALUES FOUND' if shared else 'all five differ  OK'}")
    elif not any(cur.get("KOTAK_" + f) for f in NEEDED):
        print("\n  WARNING: the strangle's KOTAK_* are not in this .env, so the")
        print("  same-account check could not run. Pass --compare <strangle creds file>")
        print("  to check properly, or run this on the machine that holds both.")
    if clashes:
        print("\n" + "!" * 66)
        print("REFUSING TO WRITE — these match the strangle's KOTAK_* values:")
        for c in clashes:
            print(f"    KOTAK_DM_{c} == KOTAK_{c}")
        print("That is the SAME Kotak account. DualMom logging in would steal the")
        print("strangle's session and it would lose its 15:14 square-off.")
        print("!" * 66)
        sys.exit(1)
    print("  no collision with the strangle's KOTAK_* values  OK")

    already = [f for f in NEEDED if cur.get("KOTAK_DM_" + f)]
    if already:
        print(f"\n  note: overwriting existing {', '.join('KOTAK_DM_' + a for a in already)}")

    if not write:
        print("\n" + "=" * 66)
        print("PREVIEW ONLY — nothing written. Re-run with --write to apply.")
        print("=" * 66)
        return

    # --- write ---
    ENV.parent.mkdir(parents=True, exist_ok=True)
    if ENV.exists():
        bak = ENV.with_suffix(f".env.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(ENV, bak)
        print(f"\n  backed up .env -> {bak.name}")
        lines = ENV.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        lines = []

    for f in NEEDED:
        key, val = "KOTAK_DM_" + f, found[f].replace(" ", "") if f in ("MOBILE", "TOTP_SECRET") else found[f]
        for i, line in enumerate(lines):
            if re.match(rf"^\s*{key}\s*=", line):
                lines[i] = f"{key}={val}"
                break
        else:
            lines.append(f"{key}={val}")
    if lines and lines[0].strip() != "" and not any(l.startswith("# DualMom") for l in lines):
        lines.insert(len(lines) - len(NEEDED), "")
        lines.insert(len(lines) - len(NEEDED), "# DualMom Kotak account — SEPARATE from the strangle's KOTAK_*")

    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  wrote {len(NEEDED)} KOTAK_DM_* entries to {ENV}")
    print("\nNEXT:")
    print("  .venv\\Scripts\\python.exe -m deployment.dualmom_live.kotak_auth_dm")
    print("  (that is go-live step 1 — it should print 'login OK')")
    print("\nThen DELETE the plain-text credential copy:")
    print(f"  del \"{src}\"")


if __name__ == "__main__":
    main()
