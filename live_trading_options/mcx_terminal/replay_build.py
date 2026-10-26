"""
mcx_terminal/replay_build.py — turn a day's recording into ONE double-clickable HTML file.
=========================================================================================

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.replay_build 2026-10-26
    -> data\\replays\\MCX_2026-10-26.html   (opens offline in Chrome; no server, no login)

The file holds the SAME screen code as the live terminal (app.css + app.js) plus replay.js, which
stands in for the engine connection and feeds the recorded messages back at their recorded times.
Same code + same messages = the same screen.

Data inside the file (gzip, base64):
  ticks  — columnar binary: t_ms offset (uint32), token index (uint16), then the 11 tick fields as
           float32 columns (NaN = not sent; last-update as seconds since the start)
  events — JSON [[t_ms offset, kind, payload], ...]: header, chain, book, risk, status, settings, mtm
Only contracts the screen could show are kept: the recorded chains (every strike), the futures
and anything held or ordered.
"""
import base64
import gzip
import json
import logging
import sys
from pathlib import Path

import numpy as np

from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal.recorder import REPLAY_DIR, read_raw

log = logging.getLogger("mcx.replay")
FIELDS = 11            # tick fields after the token: ltp bid ask bq aq vol oi pc ltq lut chg
LUT_COL = 9            # last-update epoch seconds -> stored as seconds since t0 (keeps float32 exact)


def _b64gz(b: bytes) -> str:
    return base64.b64encode(gzip.compress(b, 6)).decode("ascii")


def build(day: str, folder: Path | None = None) -> Path:
    folder = folder or REPLAY_DIR
    raw = folder / f"raw_{day}.mcxr"
    if not raw.exists():
        raise FileNotFoundError(raw)
    ticks, events = read_raw(raw)
    headers = [e for e in events if e[1] == "header"]
    if not headers:
        raise RuntimeError("recording has no header (no order was placed that day?)")
    hdr = headers[0][2]

    # contracts the screen could show
    keep = set()
    for ch in hdr.get("chains", []) + [e[2] for e in events if e[1] == "chain"]:
        for r in ch["rows"]:
            keep.update((r["ce"], r["pe"]))
        if ch.get("fut"):
            keep.add(ch["fut"]["token"])
    for p in hdr.get("meta", {}).get("products", []):
        keep.update(f["token"] for f in p["futures"])
    for e in events:
        if e[1] == "book":
            keep.update(p["token"] for p in e[2]["positions"])
            keep.update(o["token"] for o in e[2]["orders"])
    ticks = [t for t in ticks if t[1] in keep]
    last = [r for r in hdr.get("last", []) if r[0] in keep]

    t0 = min(([ticks[0][0]] if ticks else []) + ([events[0][0]] if events else []))
    t1 = max(([ticks[-1][0]] if ticks else []) + ([events[-1][0]] if events else []))
    toks = sorted({t[1] for t in ticks} | {r[0] for r in last})
    tix = {t: i for i, t in enumerate(toks)}
    n = len(ticks)
    tt = np.empty(n, dtype=np.uint32)
    ti = np.empty(n, dtype=np.uint16)
    vals = np.full((n, FIELDS), np.nan, dtype=np.float32)   # exact for MCX prices / lots; lut stored as offset
    for k, r in enumerate(ticks):
        tt[k] = r[0] - t0
        ti[k] = tix[r[1]]
        for j in range(FIELDS):
            v = r[2 + j]
            if v is not None:
                vals[k, j] = v - t0 // 1000 if j == LUT_COL else v
    blob = tt.tobytes() + ti.tobytes() + vals.T.copy().tobytes()      # column-major: one field after another

    ev = [[e[0] - t0, e[1], e[2]] for e in events]
    info = {"day": day, "t0": t0, "t1": t1, "n": n, "fields": FIELDS, "tokens": toks, "last": last}

    static = C.STATIC
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    app = (static / "app.js").read_text(encoding="utf-8")
    rjs = (static / "replay.js").read_text(encoding="utf-8")
    rcss = (static / "replay.css").read_text(encoding="utf-8")
    import re
    html = re.sub(r'<link rel="stylesheet" href="/static/app.css[^"]*">',
                  lambda _: f"<style>\n{css}\n{rcss}\n</style>", html)
    html = re.sub(r"<title>.*?</title>", f"<title>MCX Desk replay {day}</title>", html)
    payload = (f'<script id="rp-info" type="application/json">{json.dumps(info, separators=(",", ":"))}</script>\n'
               f'<script id="rp-ticks" type="application/octet-stream">{_b64gz(blob)}</script>\n'
               f'<script id="rp-events" type="application/octet-stream">'
               f'{_b64gz(json.dumps(ev, separators=(",", ":"), default=str).encode())}</script>\n'
               f"<script>\n{rjs}\n</script>\n<script>\n{app}\n</script>")
    html = re.sub(r'<script src="/static/app.js[^"]*"></script>', lambda _: payload, html)
    out = folder / f"MCX_{day}.html"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(out)
    log.info("replay built: %s  ticks %d  tokens %d  span %.1f min  %.1f MB",
             out.name, n, len(toks), (t1 - t0) / 60000, out.stat().st_size / 1e6)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.stdout.reconfigure(encoding="utf-8")
    d = sys.argv[1] if len(sys.argv) > 1 else None
    if not d:
        import datetime as _dt
        d = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5, minutes=30))).date().isoformat()
    p = build(d)
    print(p)
