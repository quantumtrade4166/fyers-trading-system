"""
cepe_imbalance/diag.py — see what the engine's threads are doing (read-only; changes nothing).
=============================================================================================

17-Nov-2026. Two tools, so a freeze never again has to be guessed at:

  watchdog   every second: if the tick worker, the timer or a live-book worker has been inside ONE step
             for more than STALL_S, write where each of them is standing (file / line / function) to the
             log — once per freeze, and again when it ends with how long it lasted.
  profile()  for N seconds, look at every thread 100 times a second and count where it was. Threads that
             are only waiting (sleep, lock, socket) are counted as idle. GET /debug/prof?secs=5
"""
import collections
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger("cepe.diag")

STALL_S = 2.0
LIVE_STALL_S = 10.0
_IDLE = {"wait", "acquire", "sleep", "select", "recv", "recv_into", "read", "get", "accept", "_recv",
         "poll", "_poll", "run_forever", "readinto", "_wait_for_tstate_lock", "join", "getaddrinfo",
         "GetQueuedCompletionStatus", "_read", "recv_strict", "recv_frame", "recv_data_frame"}


def _names() -> dict:
    return {t.ident: t.name for t in threading.enumerate()}


def stacks(only=None, depth: int = 14) -> dict:
    """thread name -> its call stack as text lines (innermost last)."""
    names, out = _names(), {}
    for ident, frame in sys._current_frames().items():
        nm = names.get(ident, str(ident))
        if only and not any(nm.startswith(p) for p in only):
            continue
        rows = traceback.extract_stack(frame)[-depth:]
        out[nm] = [f"{r.filename.replace(chr(92), '/').split('/')[-1]}:{r.lineno} {r.name}" for r in rows]
    return out


def profile(secs: float = 5.0, hz: int = 100, top: int = 12) -> dict:
    """Where every thread spent `secs` seconds. busy_pct = share of samples NOT standing in a wait."""
    secs = max(0.2, min(float(secs), 20.0))
    me = threading.get_ident()
    names = _names()
    leaf: dict = collections.defaultdict(collections.Counter)      # thread -> "file:func" (innermost)
    path: dict = collections.defaultdict(collections.Counter)      # thread -> 4 innermost frames
    busy: collections.Counter = collections.Counter()
    n = 0
    end = time.perf_counter() + secs
    while time.perf_counter() < end:
        for ident, f in sys._current_frames().items():
            if ident == me:
                continue
            nm = names.get(ident) or _names().get(ident, str(ident))
            co = f.f_code
            if co.co_name in _IDLE:
                leaf[nm]["(idle)"] += 1
                continue
            busy[nm] += 1
            leaf[nm][f"{co.co_filename.replace(chr(92), '/').split('/')[-1]}:{co.co_name}:{f.f_lineno}"] += 1
            chain, g = [], f
            while g is not None and len(chain) < 4:
                chain.append(f"{g.f_code.co_filename.replace(chr(92), '/').split('/')[-1]}:{g.f_code.co_name}")
                g = g.f_back
            path[nm][" < ".join(chain)] += 1
        n += 1
        time.sleep(1.0 / hz)
    threads = []
    for nm in sorted(leaf, key=lambda k: -busy[k]):
        if not busy[nm]:
            continue
        threads.append({"thread": nm, "busy_pct": round(100.0 * busy[nm] / max(n, 1), 1),
                        "where": [[k, round(100.0 * v / max(n, 1), 1)] for k, v in leaf[nm].most_common(top) if k != "(idle)"],
                        "paths": [[k, round(100.0 * v / max(n, 1), 1)] for k, v in path[nm].most_common(6)]})
    return {"secs": secs, "samples": n, "threads_seen": len(leaf),
            "total_busy_pct": round(100.0 * sum(busy.values()) / max(n, 1), 1), "threads": threads}


def start_watchdog(engine, feed_of):
    """feed_of() -> the Kotak MultiFeed (has .busy_since). Logs a freeze once, with every worker's position."""
    state = {"what": None, "since": 0.0}

    def stuck():
        now = time.time()
        f = feed_of()
        b = getattr(f, "busy_since", 0.0) if f else 0.0
        if b and now - b > STALL_S:
            return "tick worker", b
        b = getattr(engine, "timer_busy", 0.0)
        if b and now - b > STALL_S:
            return "timer", b
        for nm, t in list(engine.live_busy.items()):
            if now - t > LIVE_STALL_S:                       # a Kotak order (send, poll, re-price) takes seconds
                return f"live book {nm}", t
        return None, 0.0

    def loop():
        while True:
            time.sleep(1.0)
            try:
                what, since = stuck()
                if what and state["what"] is None:
                    state.update(what=what, since=since)
                    st = stacks(only=("CepeTicks", "CepeTimer", "CepeLive", "CepeWriter", "DeltaTicks", "CepeDelta"))
                    log.warning("STALL: %s inside one step for %.1f s. Where each worker is:\n%s", what,
                                time.time() - since,
                                "\n".join(f"  [{k}] " + " > ".join(v[-7:]) for k, v in sorted(st.items())))
                elif not what and state["what"] is not None:
                    log.warning("STALL over: %s was stuck about %.1f s", state["what"], time.time() - state["since"])
                    state.update(what=None, since=0.0)
            except Exception:
                log.exception("watchdog")

    threading.Thread(target=loop, daemon=True, name="CepeWatchdog").start()


# ── memory cleaner (17-Nov-2026) ────────────────────────────────────────────
# The engine keeps the whole day of chart samples in memory: by the afternoon that is millions of small
# objects. Python's automatic clean-up ("full collection") stops EVERY thread while it looks at all of
# them: ~1 s on a fast PC with a full day loaded, several seconds on the VPS. In that pause the price
# reader, the rules and the live-order workers all stand still (it showed as the Kotak and the Delta
# worker both exactly N seconds behind, and as a dropped feed: the keep-alive reply was read too late).
# gc.freeze() tells the cleaner that everything alive right now is permanent, so it never looks at it
# again; only what was created since the last freeze is checked, which takes milliseconds.
GC = {"full": 0, "worst_ms": 0.0, "last_ms": 0.0, "slow": 0, "frozen": 0, "freezes": 0}
_gc_t0 = [0.0]


def _gc_cb(phase, info):
    if info.get("generation") != 2:
        return
    if phase == "start":
        _gc_t0[0] = time.perf_counter()
        return
    ms = (time.perf_counter() - _gc_t0[0]) * 1000.0
    GC["full"] += 1
    GC["last_ms"] = round(ms, 1)
    if ms > GC["worst_ms"]:
        GC["worst_ms"] = round(ms, 1)
    if ms > 250:
        GC["slow"] += 1
        log.warning("memory clean-up paused every thread for %.0f ms", ms)


def gc_guard(period: float = 5.0, start: bool = True):
    """Measure every full collection and keep them short (freeze now, then every `period` seconds)."""
    import gc
    if _gc_cb not in gc.callbacks:
        gc.callbacks.append(_gc_cb)

    def freeze():
        gc.freeze()                                          # cheap: no walk over the objects
        GC["freezes"] += 1
    freeze()
    GC["frozen"] = gc.get_freeze_count()                     # counted once (the count itself walks them all)
    if not start:
        return freeze

    def loop():
        while True:
            time.sleep(period)
            try:
                freeze()
            except Exception:
                log.exception("gc freeze")
    threading.Thread(target=loop, daemon=True, name="CepeGcGuard").start()
    return freeze
