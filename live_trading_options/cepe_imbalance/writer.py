"""
cepe_imbalance/writer.py — PAPER-book and chart files are written by ONE background thread.
==========================================================================================

17-Nov-2026. The feed worker and the 0.5 s timer used to write files themselves (events, book, state,
26 history rows every half second), some of it while holding the engine lock. When the disk was busy
(the 10-minute VPS backup, a virus scan holding a file) those writes waited — and every book's prices,
rules and the screen waited with them (16-Nov 20:33: one tick batch stuck 14.5 s, feed dropped twice).

Now the caller only hands over the finished text; the disk is touched here. Order is kept per file
(appends in the order given; a whole-file replace keeps only the newest text).

NOT started (tests, tools, replays)  -> every call writes at once, exactly as before.
LIVE books never come here: their files are the record of real orders and stay written on the spot.
"""
import collections
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("cepe.writer")

GATHER_S = 0.5          # chart rows wait at most this long; book / state / event writes wake the thread at once
RETRIES = 40            # x 0.05 s while another program holds the file


class Writer:
    def __init__(self):
        self._q = collections.deque()             # ("a", path, text) | ("r", path)
        self._latest: dict = {}                   # path -> newest whole-file text not yet written
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._dirs: set = set()
        self._written: dict = {}                  # path -> text already on disk (same text again = nothing to do)
        self.started = False
        self.stats = {"appends": 0, "replaces": 0, "same": 0, "errors": 0, "slowest_s": 0.0, "max_queue": 0}

    # ── callers (any thread; never touch the disk when started) ─────────────
    def append(self, path, text: str, wake: bool = True):
        if not self.started:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text)
            return
        with self._lock:
            self._q.append(("a", str(path), text))
            self._idle.clear()
        if wake:
            self._wake.set()

    def replace(self, path, text: str) -> bool:
        if not self.started:
            from live_trading_options.cepe_imbalance.book import atomic_write
            return atomic_write(Path(path), text)
        p = str(path)
        with self._lock:
            if p not in self._latest and self._written.get(p) == text:
                self.stats["same"] += 1                    # the rules re-save an unchanged state on every tick
                return True
            if p not in self._latest:
                self._q.append(("r", p))
            self._latest[p] = text
            self._idle.clear()
        self._wake.set()
        return True

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until everything handed over so far is on disk (reset, shutdown, tests)."""
        if not self.started:
            return True
        self._wake.set()
        return self._idle.wait(timeout)

    def forget(self):
        """Files were moved away (reset): the next save of each is written even if its text is the same."""
        with self._lock:
            self._written.clear()
        self._dirs.clear()

    def start(self):
        if self.started:
            return
        self.started = True
        threading.Thread(target=self._loop, daemon=True, name="CepeWriter").start()

    def status(self) -> dict:
        return {**self.stats, "queued": len(self._q), "started": self.started}

    # ── the writer thread ───────────────────────────────────────────────────
    def _loop(self):
        while True:
            self._wake.wait(GATHER_S)
            self._wake.clear()
            try:
                self._drain()
            except Exception:
                log.exception("writer")
                time.sleep(0.5)

    def _drain(self):
        while True:
            with self._lock:
                if not self._q:
                    self._idle.set()
                    return
                n = len(self._q)
                if n > self.stats["max_queue"]:
                    self.stats["max_queue"] = n
                jobs = [self._q.popleft() for _ in range(n)]
                texts = {j[1]: self._latest.pop(j[1]) for j in jobs if j[0] == "r"}
            t0 = time.perf_counter()
            run: dict = {}                               # consecutive appends to one file -> one write
            for j in jobs:
                if j[0] == "a":
                    run.setdefault(j[1], []).append(j[2])
                else:
                    self._appends(run, only=j[1])        # anything queued for this file before the replace
                    self._replace(j[1], texts[j[1]])
            self._appends(run)
            dt_ = time.perf_counter() - t0
            if dt_ > self.stats["slowest_s"]:
                self.stats["slowest_s"] = round(dt_, 3)
            if dt_ > 2.0:
                log.warning("disk was slow: %d writes took %.1f s (nothing waited on it)", len(jobs), dt_)

    def _mkdir(self, path):
        d = os.path.dirname(path)
        if d not in self._dirs:
            os.makedirs(d, exist_ok=True)
            self._dirs.add(d)

    def _try(self, fn, what) -> bool:
        for i in range(RETRIES):
            try:
                fn()
                return True
            except PermissionError:
                time.sleep(0.05)
            except FileNotFoundError:
                self._dirs.clear()                       # folder moved away (reset archive): make it again
                time.sleep(0.02)
            except Exception:
                log.exception("write %s", what)
                break
        self.stats["errors"] += 1
        log.warning("could not write %s", what)
        return False

    def _appends(self, run: dict, only=None):
        for p in ([only] if only else list(run)):
            parts = run.pop(p, None)
            if not parts:
                continue

            def go(p=p, text="".join(parts)):
                self._mkdir(p)
                with open(p, "a", encoding="utf-8") as fh:
                    fh.write(text)
            self._try(go, p)
            self.stats["appends"] += len(parts)

    def _replace(self, p, text):
        tmp = os.path.splitext(p)[0] + ".tmp"

        def write():
            self._mkdir(p)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
        ok = self._try(write, tmp) and self._try(lambda: os.replace(tmp, p), p)
        with self._lock:
            if ok:
                self._written[p] = text
            else:
                self._written.pop(p, None)
            if len(self._written) > 600:                     # old days
                self._written.clear()
        self.stats["replaces"] += 1


W = Writer()
