#!/usr/bin/env python
"""
rebuild_desktop_index.py  --  Disaster-recovery tool for the Claude Desktop sidebar.

The Claude Desktop app lists Code sessions by scanning the filesystem for
per-session index files (NO database):

    <Claude app data>\claude-code-sessions\<accountId>\<orgId>\local_<uuid>.json

The actual transcripts are read from the canonical CLI location:

    C:\Users\<you>\.claude\projects\<cwd-encoded>\<cliSessionId>.jsonl

If the index files are lost (app reinstall, corruption, machine migration) the
sidebar goes empty even though every transcript is intact. This script rebuilds
one index file per transcript so the sidebar repopulates with correct titles,
project paths, models and timestamps.

Usage (defaults are filled in for this machine):
    python rebuild_desktop_index.py            # dry run, prints what it would create
    python rebuild_desktop_index.py --write     # actually create missing index files

Override paths if account/org/locations differ (e.g. on a new machine):
    python rebuild_desktop_index.py --write \
        --projects "C:\Users\PC\.claude\projects" \
        --account 54037dfb-2850-4ab5-a96a-4bb6854d9966 \
        --org 8d5e313b-51a3-4d77-8984-f01123ff97ed
"""
import json, os, glob, sys, uuid, re, argparse
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_APPDATA = r"C:\Users\PC\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude"
DEFAULT_PROJECTS = r"C:\Users\PC\.claude\projects"
DEFAULT_ACCOUNT = "54037dfb-2850-4ab5-a96a-4bb6854d9966"
DEFAULT_ORG = "8d5e313b-51a3-4d77-8984-f01123ff97ed"

# Hand-written titles for sessions whose opening message is uninformative.
OVERRIDES = {
    "54a0f0aa-6ec3-4c76-8e67-0965f042838a": "Nifty 500 daily data setup",
    "5684f41c-fced-4d4c-b7e7-2dcf14782cf2": "Add DualMom dashboard",
    "954d3510-41e6-48d9-b8c6-a6bebb821692": "fyers_data_pipeline session (Jun 20)",
    "e7830272-0b80-41fb-94b5-61c8a5a0dc4f": "Fix settings.py FYERS credentials",
}


def to_ms(iso):
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def clean_title(text, cwd, created):
    t = text or ""
    t = re.sub(r"[#*`>_]", " ", t).replace("—", " — ")
    t = re.sub(r"(?i)^[\s\.\-,]*(yo+\b)?[\s\.\-,]*", "", t)
    t = re.sub(r"(?i)^(start session|start a session|start)[\s\.\-:,]*", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -—.,:")
    t = re.split(r"[\.\n]", t)[0]
    if len(t) > 60:
        t = t[:60].rsplit(" ", 1)[0] + "…"
    if (not t) or len(t) < 12 or re.match(r"^[A-Za-z]:[\\/]", t):
        d = datetime.fromisoformat(created.replace("Z", "+00:00")).strftime("%b %d") if created else "?"
        base = os.path.basename(cwd.rstrip("\\")) if cwd else "session"
        t = f"Session — {base} ({d})"
    return t[0].upper() + t[1:]


def extract(path):
    cwd = created = last = model = None
    cands = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            ts = o.get("timestamp")
            if ts:
                last = ts
                if created is None:
                    created = ts
            if cwd is None and o.get("cwd"):
                cwd = o["cwd"]
            if model is None and o.get("type") == "assistant":
                m = o.get("message", {}).get("model")
                if m:
                    model = m
            if len(cands) < 8 and o.get("type") == "user":
                c = o.get("message", {}).get("content")
                txt = None
                if isinstance(c, str):
                    txt = c
                elif isinstance(c, list):
                    for p in c:
                        if isinstance(p, dict) and p.get("type") == "text":
                            txt = p["text"]
                            break
                if txt and not txt.startswith("<"):
                    cl = re.sub(r"(?i)^[\s\.\-,]*(yo+\b)?[\s\.\-,]*", "", txt)
                    cl = re.sub(r"(?i)^(start a session|start session|start)[\s\.\-:,]*", "", cl).strip()
                    if cl:
                        cands.append(cl)
    pick = next((c for c in cands if len(c) >= 20), None) or (max(cands, key=len) if cands else None)
    return cwd, created, last, model, pick


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--appdata", default=DEFAULT_APPDATA)
    ap.add_argument("--projects", default=DEFAULT_PROJECTS)
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    ap.add_argument("--org", default=DEFAULT_ORG)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    orgdir = os.path.join(args.appdata, "claude-code-sessions", args.account, args.org)
    if args.write:
        os.makedirs(orgdir, exist_ok=True)

    # Which cliSessionIds already have an index file? (avoid duplicates)
    existing = set()
    for jf in glob.glob(os.path.join(orgdir, "local_*.json")):
        try:
            existing.add(json.load(open(jf, encoding="utf-8")).get("cliSessionId"))
        except Exception:
            pass

    transcripts = glob.glob(os.path.join(args.projects, "*", "*.jsonl"))
    created_n = skipped_n = 0
    for f in sorted(transcripts):
        sid = os.path.basename(f)[:-6]
        if sid in existing:
            skipped_n += 1
            continue
        cwd, created, last, model, pick = extract(f)
        title = OVERRIDES.get(sid) or clean_title(pick, cwd, created)
        obj = {
            "sessionId": "local_" + str(uuid.uuid4()),
            "cliSessionId": sid,
            "cwd": cwd,
            "originCwd": cwd,
            "lastFocusedAt": to_ms(last),
            "createdAt": to_ms(created),
            "lastActivityAt": to_ms(last),
            "model": model or "claude-opus-4-8",
            "effort": "high",
            "isArchived": False,
            "title": title,
            "titleSource": "auto",
            "permissionMode": "default",
            "remoteMcpServersConfig": [],
            "alwaysAllowedReasons": [],
            "sessionPermissionUpdates": [],
            "classifierSummaryEnabled": True,
            "spawnSeed": {},
        }
        print(f"[NEW] {sid[:8]}  {str(cwd):<26}  {title}")
        if args.write:
            out = os.path.join(orgdir, obj["sessionId"] + ".json")
            with open(out, "w", encoding="utf-8") as w:
                json.dump(obj, w, ensure_ascii=False, separators=(",", ":"))
        created_n += 1

    mode = "WRITTEN" if args.write else "(dry run — pass --write to apply)"
    print(f"\n{created_n} new index files {mode}; {skipped_n} already indexed.")
    if not args.write and created_n:
        print("Then fully quit and relaunch Claude Desktop.")


if __name__ == "__main__":
    main()
