"""
mcx_terminal/cpumeter.py - which THREAD is using the CPU (21-Nov-2026).
======================================================================

Python runs one thread at a time, so when the program has no spare time the screen freezes (NSE desk,
20-Nov 14:31-14:35: stalls of 1-2.6 s while ~15 orders went out). The stall log shows where the web loop
was waiting, not who was using the time. This meter asks Windows for each thread's own CPU time
(GetThreadTimes) every 2 s and logs:

  CPU HOT  - a 2-s window above HOT_CORES: the busiest threads, their share and what each is doing now
  cpu 1m   - once a minute: the program's average load and its top threads (a baseline to compare with)

Read-only and cheap (one Windows call per thread every 2 s). Off on non-Windows.
"""
import ctypes
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger("mcx.cpu")

WINDOW_S = 2.0
HOT_CORES = 0.6          # a 2-s window busier than this is logged with the threads behind it
HOT_EVERY_S = 10.0       # at most one CPU HOT line per this many seconds
SUMMARY_S = 60.0
_THREAD_QUERY = 0x0800   # THREAD_QUERY_LIMITED_INFORMATION


class _FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]


def _thread_cpu(k32, native_id):
    """Seconds of CPU (user + kernel) the thread has used so far, or None."""
    h = k32.OpenThread(_THREAD_QUERY, False, native_id)
    if not h:
        return None
    try:
        c, e, kt, ut = _FT(), _FT(), _FT(), _FT()
        if not k32.GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
            return None
        return (((kt.hi << 32) | kt.lo) + ((ut.hi << 32) | ut.lo)) / 1e7
    finally:
        k32.CloseHandle(h)


def _where(frame) -> str:
    if frame is None:
        return "?"
    top = traceback.extract_stack(frame, limit=5)
    return " > ".join(f"{x.filename.split(chr(92))[-1]}:{x.lineno}:{x.name}" for x in top[-3:])


def _run():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenThread.restype = ctypes.c_void_p
    k32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.GetThreadTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    prev: dict = {}                      # native id -> cpu seconds
    acc: dict = {}                       # thread name -> cpu seconds this minute
    proc_prev, t_prev = time.process_time(), time.time()
    min_proc, min_t = proc_prev, t_prev
    last_hot = 0.0
    me = threading.get_native_id()
    while True:
        time.sleep(WINDOW_S)
        try:
            now, proc = time.time(), time.process_time()
            span = max(now - t_prev, 1e-3)
            threads = {th.native_id: th for th in threading.enumerate() if th.native_id and th.native_id != me}
            used = []
            for nid, th in threads.items():
                cpu = _thread_cpu(k32, nid)
                if cpu is None:
                    continue
                d = cpu - prev.get(nid, cpu)
                prev[nid] = cpu
                if d > 0:
                    used.append((d, th))
                    acc[th.name] = acc.get(th.name, 0.0) + d
            for nid in list(prev):
                if nid not in threads:
                    prev.pop(nid)
            cores = (proc - proc_prev) / span
            proc_prev, t_prev = proc, now
            if cores >= HOT_CORES and now - last_hot >= HOT_EVERY_S:
                last_hot = now
                used.sort(key=lambda x: -x[0])
                frames = sys._current_frames()
                named = sum(d for d, _ in used)
                parts = [f"[{th.name}] {d / span:.2f} @ {_where(frames.get(th.ident))}" for d, th in used[:6]]
                other = max(cores - named / span, 0.0)
                log.warning("CPU HOT %.2f cores over %.1fs (threads Python does not know: %.2f): %s",
                            cores, span, other, " || ".join(parts))
            if now - min_t >= SUMMARY_S:
                avg = (proc - min_proc) / (now - min_t)
                top = sorted(acc.items(), key=lambda kv: -kv[1])[:5]
                log.info("cpu 1m: %.2f cores; top: %s", avg,
                         ", ".join(f"{n} {s / (now - min_t):.2f}" for n, s in top))
                acc, min_proc, min_t = {}, proc, now
        except Exception:
            log.exception("cpu meter")
            time.sleep(10)


def start():
    if sys.platform != "win32":
        return
    threading.Thread(target=_run, daemon=True, name="CpuMeter").start()
