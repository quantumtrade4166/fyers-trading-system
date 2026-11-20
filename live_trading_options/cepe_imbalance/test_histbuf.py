"""HistBuf (compact chart samples, 20-Nov-2026) must answer exactly like the old list of dicts.

    .venv\\Scripts\\python.exe -m live_trading_options.cepe_imbalance.test_histbuf [history.jsonl]
"""
import bisect
import json
import random
import sys
import threading
import time
import types

sys.stdout.reconfigure(encoding="utf-8")
from live_trading_options.cepe_imbalance import days
from live_trading_options.cepe_imbalance import service as S
from live_trading_options.cepe_imbalance.histbuf import HistBuf

FAIL = 0
PASS = 0


def check(name, ok, info=""):
    global FAIL, PASS
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL", name, str(info)[:300])


def same(a, b):
    """Rows equal as JSON would show them (int 3 and float 3.0 are the same number)."""
    return json.loads(json.dumps(a)) == json.loads(json.dumps(b))


def synth(n=3000, seed=1):
    rnd = random.Random(seed)
    t, rows = 1795000000.0, []
    for i in range(n):
        t += round(rnd.uniform(0.3, 0.7), 2)
        r = {"t": round(t, 2), "mtm": round(rnd.uniform(-500, 500), 2), "imb": None if i % 7 == 0 else rnd.uniform(-40, 40),
             "ce": rnd.uniform(0, 900), "pe": rnd.uniform(0, 900), "ref": 25000.5, "atm": 25000.0 + 50 * (i % 3)}
        if i % 2:
            r["st"] = rnd.uniform(24000, 26000)
            r["st_red"] = bool(i % 4 == 1)
        if i % 5:
            r["ace"], r["ape"] = rnd.uniform(1, 200), (None if i % 11 == 0 else rnd.uniform(1, 200))
        if i % 3:
            r["sp"] = [None if (i + k) % 9 == 0 else round(rnd.uniform(0, 30), 2) for k in range(6)]
        if i == 17:
            r["odd_key"] = {"x": 1}                    # unknown key: kept verbatim
        if i == 23:
            r["sp"] = [1.0, 2.0]                       # unexpected shape: kept verbatim
        rows.append(r)
    return rows


def _check_roundtrip(rows):
    h = HistBuf(rows)
    check("same length", len(h) == len(rows))
    check("every row identical", all(same(h[i], rows[i]) for i in range(len(rows))))
    check("negative index", same(h[-1], rows[-1]))
    check("slices", same(h[10:50], rows[10:50]) and same(h[:], rows))
    check("iteration", same(list(h), rows))
    check("odd key + odd sp kept", "odd_key" in h[17] if len(rows) > 17 else True)


def _check_downsample(rows):
    h = HistBuf(rows)
    for mp in (100, 800, 8000, len(rows) + 5):
        check(f"downsample {mp} identical", same(days.downsample(h, mp), days.downsample(list(rows), mp)))


def _check_since(rows):
    h = HistBuf(rows)
    key = lambda r: r["t"]
    for t in (rows[0]["t"] - 1, rows[len(rows) // 2]["t"], rows[-1]["t"], rows[-1]["t"] + 5):
        want = rows[bisect.bisect_right(rows, t, key=key):]
        check(f"since {t}", same(h.since(t), want) and h.index_after(t) == len(rows) - len(want))


def _check_trim(rows):
    h, ref = HistBuf(rows), list(rows)
    h.trim(1000, slack=0)
    check("trim keeps newest", same(h[:], ref[-1000:]))
    h2 = HistBuf(rows)
    h2.trim(len(rows) - 5, slack=10)
    check("slack: no trim under the slack", len(h2) == len(rows))
    h3 = HistBuf(rows)
    del h3[:-500]
    check("del buf[:-n] like a list", same(h3[:], ref[-500:]))
    h3.append(dict(ref[17]))
    check("extra survives trims", "odd_key" in h3[-1] if "odd_key" in ref[17] else True)


def _check_day_stats(rows):
    """service._day_stats on a HistBuf == on the old list, also when samples arrive in pieces."""
    for split in (0, len(rows) // 3, len(rows) - 1):
        lst, hb = list(rows[:split]), HistBuf(rows[:split])
        e = types.SimpleNamespace(runners={}, hist={"A": lst, "B": hb})
        S.E = e
        S._day_run.clear()
        a1, b1 = S._day_stats("A"), S._day_stats("B")
        for r in rows[split:]:
            lst.append(r)
            hb.append(r)
        a2, b2 = S._day_stats("A"), S._day_stats("B")
        check(f"day stats equal (split {split})", same(a1, b1) and same(a2, b2), (a2, b2))


def _check_threads(rows):
    h = HistBuf()
    stop = threading.Event()
    errs = []

    def writer():
        for r in rows:
            h.append(r)
            if len(h) > 1500:
                h.trim(1000, slack=200)
        stop.set()

    def reader():
        while not stop.is_set():
            try:
                d = days.downsample(h, 200)
                assert all("t" in x for x in d)
                h.since(rows[0]["t"])
            except Exception as e:                       # noqa: BLE001
                errs.append(repr(e))
    ths = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    check("append / trim / read from 4 threads: no errors", not errs, errs[:3])


def _check_real_file(path):
    rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]
    import tracemalloc
    tracemalloc.start()
    h = HistBuf(rows)
    hb = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    tracemalloc.start()
    l2 = [json.loads(json.dumps(r)) for r in rows]
    lb = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    check("real file: rows identical", all(same(h[i], rows[i]) for i in range(len(rows))))
    check("real file: chart identical", same(days.downsample(h, 8000), days.downsample(rows, 8000)))
    t = time.perf_counter(); days.downsample(rows, 8000); a = time.perf_counter() - t
    t = time.perf_counter(); days.downsample(h, 8000); b = time.perf_counter() - t
    print(f"real file {len(rows):,} rows: memory {lb/1e6:.0f} MB as dicts -> {hb/1e6:.0f} MB compact "
          f"({lb/max(hb,1):.1f}x smaller); chart {a*1000:.0f} ms -> {b*1000:.0f} ms")
    del l2


def test_histbuf_matches_list():
    """pytest entry: every check on synthetic rows (a real file can be given when run directly)."""
    global FAIL, PASS
    FAIL = PASS = 0
    rows = synth()
    _check_roundtrip(rows)
    _check_downsample(rows)
    _check_since(rows)
    _check_trim(rows)
    _check_day_stats(rows)
    _check_threads(synth(6000, seed=2))
    assert FAIL == 0, f"{FAIL} HistBuf checks failed"


if __name__ == "__main__":
    rows = synth()
    _check_roundtrip(rows)
    _check_downsample(rows)
    _check_since(rows)
    _check_trim(rows)
    _check_day_stats(rows)
    _check_threads(synth(6000, seed=2))
    if len(sys.argv) > 1:
        _check_real_file(sys.argv[1])
    print(f"{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
