r"""
replays/replay_server.py — local backtest server for the Synthetic / Premium replay player (port 8779).
=====================================================================================================

Owner 27-Nov-2026: "run today's recorded SENSEX ticks through whatever settings I select".
Serves this folder (syn_player.html, saved replays) plus:

  GET  /api/days               recorded days: [{"und": "SENSEX", "date": "2026-11-26", "mb": 60.5}, ...]
  POST /api/run   {und, date, rules, lots, entry, exit, params{...}, changes[{at, set{...}}]}
                               runs replay_syn in a child process (its fake clock patches modules), writes
                               runs/run_NNN.json, adds it to runs/index.json, returns that entry
  GET  /api/runs               the runs made so far (newest first)

Local only (127.0.0.1). Start: "Synthetic Replay.bat" / replays\open_replay.bat.
"""
import json
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
QUOTES = ROOT / "live_trading_options" / "cepe_imbalance" / "data" / "quotes"
RUNS = HERE / "runs"
PORT = 8779
sys.path.insert(0, str(ROOT))
LOCK = threading.Lock()
PARAM_KEYS = {"strike_gap": float, "strikes": int, "start_otm": int, "mtm_stop": float, "profit_target": float,
              "lock_start": float, "lock_gap": float, "buffer": float}


def days():
    out = []
    for p in sorted(QUOTES.glob("*/*.qrec")):
        if p.stem in ("NIFTY", "SENSEX") and p.stat().st_size > 1_000_000:
            out.append({"und": p.stem, "date": p.parent.name, "mb": round(p.stat().st_size / 1e6, 1)})
    return out[::-1]


def runs():
    p = RUNS / "index.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def clean(req):
    und = str(req.get("und", "SENSEX")).upper()
    if und not in ("NIFTY", "SENSEX"):
        raise ValueError("index must be NIFTY or SENSEX")
    date = str(req.get("date", ""))
    if not (QUOTES / date / f"{und}.qrec").exists():
        raise ValueError(f"no recording for {und} {date}")
    rules = "syn2" if req.get("rules", "syn2") == "syn2" else "syn"
    lots = int(req.get("lots") or 1)
    if not 1 <= lots <= 50:
        raise ValueError("lots must be 1..50")
    hm = lambda v, d: (str(v or d) + ":00")[:8] if len(str(v or d)) == 5 else str(v or d)
    params = {k: PARAM_KEYS[k](v) for k, v in (req.get("params") or {}).items() if k in PARAM_KEYS and v not in ("", None)}
    changes = []
    for c in req.get("changes") or []:
        st = {k: (int(v) if k == "lots" else PARAM_KEYS[k](v)) for k, v in (c.get("set") or {}).items()
              if (k in PARAM_KEYS or k == "lots") and v not in ("", None)}
        if c.get("at") and st:
            changes.append({"at": hm(c["at"], "10:00"), "set": st})
    return und, date, rules, lots, hm(req.get("entry"), "09:20"), hm(req.get("exit"), "15:25"), params, changes


def run(req):
    und, date, rules, lots, entry, exit_, params, changes = clean(req)
    RUNS.mkdir(exist_ok=True)
    with LOCK:
        idx = runs()
        n = max([r["n"] for r in idx] + [0]) + 1
    out = RUNS / f"run_{n:03d}.json"
    cmd = [str(PY), "-m", "live_trading_options.cepe_imbalance.replay_syn", und, date, "--rules", rules,
           "--lots", str(lots), "--entry", entry, "--exit", exit_, "--timeline", str(out),
           "--params", json.dumps(params), "--changes", json.dumps(changes)]
    t = time.time()
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=1800)
    last = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    res = json.loads(last[-1]) if last else {"error": (p.stderr or "no output")[-800:]}
    if res.get("error") or not out.exists():
        raise RuntimeError(res.get("error") or "replay wrote no file")
    ent = {"n": n, "file": f"runs/{out.name}", "und": und, "date": date, "rules": rules, "lots": lots,
           "entry": entry, "exit": exit_, "params": params, "changes": changes, "secs": round(time.time() - t),
           "made": time.strftime("%Y-%m-%d %H:%M:%S"),
           **{k: res.get(k) for k in ("net", "gross", "charges", "adjustments", "mtm_low", "mtm_high", "entry_time")}}
    with LOCK:
        idx = runs()
        idx.insert(0, ent)
        (RUNS / "index.json").write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return ent


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(HERE), **k)

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _json(self, obj, code=200):
        b = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/sim/state"):
            import sim
            from urllib.parse import parse_qs, urlparse
            if not sim.SIM:
                return self._json({"none": True})
            q = parse_qs(urlparse(self.path).query)
            if (q.get("sid") or [""])[0] != sim.SIM.sid:
                return self._json({"other": True})
            since = int((q.get("since") or ["0"])[0])
            if (q.get("epoch") or [""])[0] != str(sim.SIM.epoch):
                since = 0                                         # rewound: the page redraws from the start
            return self._json(sim.SIM.state(since))
        if self.path.startswith("/api/days"):
            return self._json(days())
        if self.path.startswith("/api/runs"):
            return self._json(runs())
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/sim/"):
            try:
                import sim
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                if self.path.startswith("/api/sim/start"):
                    und, date, rules, lots, entry, exit_, params, _ = clean(req)
                    st = (str(req.get("start") or "09:15") + ":00")[:8] if len(str(req.get("start") or "")) == 5 else str(req.get("start") or "09:15:00")
                    s = sim.start(und=und, date=date, rules=rules, lots=lots, params=params, start=st, entry=entry, exit_=exit_)
                    s.send({"cmd": "speed", "v": req.get("speed") or 1})
                    s.send({"cmd": "play"})
                    return self._json({"ok": True, "sid": s.sid})
                if not sim.SIM:
                    return self._json({"ok": False, "error": "no simulator running - press Start"}, 400)
                if req.get("sid") != sim.SIM.sid:
                    return self._json({"ok": False, "error": "another window started a newer simulation - press Simulate live again"}, 409)
                sim.SIM.send(req)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 400)
        if not self.path.startswith("/api/run"):
            return self._json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            return self._json({"ok": True, "run": run(req)})
        except Exception as e:                                   # bad settings / no recording / replay error
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 400)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"replay server on http://127.0.0.1:{PORT}/syn_player.html", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
