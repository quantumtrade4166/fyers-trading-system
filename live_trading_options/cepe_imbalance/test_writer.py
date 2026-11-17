"""cepe_imbalance/test_writer.py — the background file writer of 17-Nov-2026 leaves the SAME files on disk
as writing on the spot, in the same order, and a busy / locked disk never holds up the caller."""
import json
import threading
import time
import types

import pytest

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import days, diag, writer
from live_trading_options.cepe_imbalance import service as S
from live_trading_options.cepe_imbalance.book import PaperBook


@pytest.fixture
def W(monkeypatch):
    """A started writer swapped in everywhere the engine uses one."""
    w = writer.Writer()
    w.start()
    from live_trading_options.cepe_imbalance import book, engine
    for m in (writer, book, engine, S):
        monkeypatch.setattr(m, "W", w)
    yield w
    w.flush()


def test_not_started_writes_on_the_spot(tmp_path):
    w = writer.Writer()
    w.append(tmp_path / "a" / "x.jsonl", "1\n")
    w.append(tmp_path / "a" / "x.jsonl", "2\n")
    assert (tmp_path / "a" / "x.jsonl").read_text() == "1\n2\n"
    assert w.replace(tmp_path / "a" / "b.json", "{}") and (tmp_path / "a" / "b.json").read_text() == "{}"
    assert w.flush() is True


def test_started_keeps_order_and_newest_replace(tmp_path, W):
    p, q = tmp_path / "d" / "h.jsonl", tmp_path / "d" / "s.json"
    for i in range(500):
        W.append(p, f"{i}\n", wake=i % 7 == 0)
        W.replace(q, json.dumps({"i": i}))
    assert W.flush(10)
    assert p.read_text().splitlines() == [str(i) for i in range(500)]
    assert json.loads(q.read_text()) == {"i": 499}
    assert not list(tmp_path.glob("d/*.tmp"))
    assert W.stats["errors"] == 0


def test_many_threads_no_line_lost_or_torn(tmp_path, W):
    p = tmp_path / "m.jsonl"

    def work(k):
        for i in range(300):
            W.append(p, json.dumps({"k": k, "i": i}) + "\n")
    ts = [threading.Thread(target=work, args=(k,)) for k in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert W.flush(10)
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    assert len(rows) == 2400
    for k in range(8):                                         # each thread's own lines stay in its order
        assert [r["i"] for r in rows if r["k"] == k] == list(range(300))


def test_a_slow_disk_does_not_hold_the_caller(tmp_path, W, monkeypatch):
    real = W._try

    def slow(fn, what):
        time.sleep(0.6)                                        # the disk is busy (backup running)
        return real(fn, what)
    monkeypatch.setattr(W, "_try", slow)
    t0 = time.perf_counter()
    for i in range(200):
        W.append(tmp_path / "s.jsonl", f"{i}\n")
        W.replace(tmp_path / "s.json", str(i))
    assert time.perf_counter() - t0 < 0.2                      # 200 writes handed over in well under 0.2 s
    assert W.flush(20)
    assert (tmp_path / "s.jsonl").read_text().splitlines() == [str(i) for i in range(200)]
    assert (tmp_path / "s.json").read_text() == "199"


def test_a_locked_file_is_retried_not_lost(tmp_path, W, monkeypatch):
    import builtins
    real_open, fails = builtins.open, {"n": 6}

    def flaky(path, mode="r", *a, **k):
        if str(path).endswith("lock.jsonl") and "a" in mode and fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError(13, "in use")
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr(builtins, "open", flaky)
    W.append(tmp_path / "lock.jsonl", "kept\n")
    assert W.flush(10)
    monkeypatch.setattr(builtins, "open", real_open)
    assert (tmp_path / "lock.jsonl").read_text() == "kept\n" and W.stats["errors"] == 0


class _Mkt:
    def __init__(self):
        self.u = types.SimpleNamespace(by_key={})

    def quote(self, key):
        return types.SimpleNamespace(ltp=100.0, bid=99.5, ask=100.5)

    def mark(self, key):
        return 100.0


def _contract(strike):
    return types.SimpleNamespace(key=f"nse_fo|{int(strike)}", symbol=f"X{int(strike)}CE", kind="CE", strike=strike,
                                 expiry="2099-01-01", lot=75, mult=1)


def test_paper_book_files_same_both_ways(tmp_path, monkeypatch, W):
    """The same fills through the writer and on the spot leave the same book + trades files."""
    def run(folder, started):
        monkeypatch.setattr(C, "DATA", folder)
        W.started = started
        b = PaperBook("NIFTY", _Mkt(), "2099-01-01")
        for i, (side, lots) in enumerate([("SELL", 2), ("SELL", 1), ("BUY", 1), ("BUY", 2), ("SELL", 3)]):
            assert b.order(_contract(23000 + 50 * (i % 2)), side, lots, reason=f"r{i}")["ok"]
        W.started = True
        assert W.flush(10)
        d = folder / "books" / "2099-01-01"
        book = json.loads((d / "NIFTY.json").read_text())
        book.pop("saved_at")
        trades = [{k: v for k, v in json.loads(x).items() if k not in ("ts", "time")}
                  for x in (d / "NIFTY_trades.jsonl").read_text().splitlines()]
        again = PaperBook("NIFTY", _Mkt(), "2099-01-01")            # a restart reloads the same book
        return book, trades, {k: (p.lots, p.avg, p.realized) for k, p in again.positions.items()}

    a = run(tmp_path / "queued", True)
    b = run(tmp_path / "direct", False)
    assert a == b and len(a[1]) == 5


def test_live_book_never_uses_the_writer(tmp_path, monkeypatch, W):
    monkeypatch.setattr(C, "DATA", tmp_path)
    calls = []
    monkeypatch.setattr(W, "append", lambda *a, **k: calls.append("append"))
    monkeypatch.setattr(W, "replace", lambda *a, **k: calls.append("replace"))
    b = PaperBook("NIFTY_LIVE", _Mkt(), "2099-01-01")
    b.live = True                                               # what KotakLiveBook sets
    assert b.order(_contract(23000), "SELL", 1)["ok"]
    d = tmp_path / "books" / "2099-01-01"
    assert calls == [] and (d / "NIFTY_LIVE.json").exists() and (d / "NIFTY_LIVE_trades.jsonl").exists()


def test_profile_and_stacks_see_a_busy_thread():
    stop = threading.Event()

    def spin():
        x = 0
        while not stop.is_set():
            x += 1
    t = threading.Thread(target=spin, name="SpinTest", daemon=True)
    t.start()
    try:
        p = diag.profile(0.4)
        me = [x for x in p["threads"] if x["thread"] == "SpinTest"]
        assert me and me[0]["busy_pct"] > 50 and any("spin" in w[0] for w in me[0]["where"])
        assert "SpinTest" in diag.stacks() and diag.stacks(only=("Spin",)).keys() == {"SpinTest"}
    finally:
        stop.set()


def test_watchdog_logs_a_stuck_tick_worker_once(monkeypatch, caplog):
    monkeypatch.setattr(diag, "STALL_S", 0.05)
    real_sleep = time.sleep
    monkeypatch.setattr(diag.time, "sleep", lambda s: real_sleep(0.02))
    feed = types.SimpleNamespace(busy_since=time.time())
    eng = types.SimpleNamespace(timer_busy=0.0, live_busy={})
    with caplog.at_level("WARNING", logger="cepe.diag"):
        diag.start_watchdog(eng, lambda: feed)
        real_sleep(0.4)
        assert sum("STALL: tick worker" in r.getMessage() for r in caplog.records) == 1
        feed.busy_since = 0.0
        real_sleep(0.2)
        assert sum("STALL over" in r.getMessage() for r in caplog.records) == 1


def test_whole_day_chart_is_shared_and_same_as_before(monkeypatch):
    from live_trading_options.cepe_imbalance import days
    rows = [{"t": 1000.0 + i * 0.5, "mtm": float(i % 97), "imb": float(i % 13)} for i in range(30000)]
    e = types.SimpleNamespace(runners={"NIFTY": types.SimpleNamespace(date="2099-01-01")}, hist={"NIFTY": rows})
    monkeypatch.setattr(S, "E", e)
    S._forget()
    a = S.history("NIFTY", max_points=8000)
    assert json.loads(a.body)["rows"] == days.downsample(list(rows), 8000)
    assert S.history("NIFTY", max_points=8000) is a             # second viewer: the same finished answer
    new = json.loads(S.history("NIFTY", since=rows[-3]["t"]).body)["rows"]
    assert new == rows[-2:]                                     # live increments are never cached
    S._forget()


# ── the whole engine, files and all: through the writer vs on the spot ────
from live_trading_options.cepe_imbalance.test_cepe import tick, universe  # noqa: E402,F401


def _engine_day(universe, folder, monkeypatch, W, started):
    import datetime as dt
    from live_trading_options.cepe_imbalance.book import IST
    from live_trading_options.cepe_imbalance.engine import Engine
    from live_trading_options.cepe_imbalance.market import Market
    monkeypatch.setattr(C, "DATA", folder)
    W.started = started
    m = Market(universe)
    eng = Engine(universe, m, persist=True)
    m.apply([tick("nse_cm|Nifty 50", 23450)])
    eng.refresh_subscriptions()
    e = m.expiry("NIFTY")
    quotes = []
    for s in m.window("NIFTY", e, 8):
        for kind, px in (("CE", 90.0), ("PE", 88.0)):
            c = m.contract("NIFTY", e, s, kind)
            if c:
                quotes.append(tick(c.key, px, px - 0.5, px + 0.5))
    eng.on_ticks(quotes)
    r = eng.runners["NIFTY"]
    day = dt.date.fromisoformat(r.date)
    assert eng.start("NIFTY")["ok"]
    for i in range(6):                                          # prices move, timer + chart samples run
        eng.on_ticks([tick(q.key, q.ltp + i, q.ltp + i - 0.5, q.ltp + i + 0.5) for q in quotes[::3]])
        eng.tick_timer(dt.datetime.combine(day, dt.time(10, i), IST))
        eng.sample(dt.datetime.combine(day, dt.time(10, i), IST))
    eng.manual_order("NIFTY", 23500, "CE", "SELL", 1)
    eng.stop("NIFTY")
    eng.reset("GOLD", start=False)                              # a reset archives + starts clean
    W.started = True
    assert W.flush(10)
    eng.stop_all()
    again = Engine(universe, m, persist=True)                   # what a restart reads back from disk
    a = again.runners["NIFTY"]
    again.stop_all()
    files = sorted(str(p.relative_to(folder)).replace("\\", "/") for p in folder.rglob("*")
                   if p.is_file() and "quotes" not in p.parts and "_reset_" not in str(p))
    return {"files": files, "running": a.running, "trades": [(t["symbol"], t["side"], t["lots"], t["price"]) for t in a.book.trades],
            "positions": {k: (p.lots, round(p.avg, 4), round(p.realized, 2)) for k, p in a.book.positions.items()},
            "events": [x["event"] for x in days.events(r.date, "NIFTY")], "hist": len(again.hist["NIFTY"]),
            "state": {k: v for k, v in a.state.items() if k != "started_at"}, "gold_events": [x["event"] for x in days.events(again.runners["GOLD"].date, "GOLD")]}


def test_engine_day_leaves_the_same_files_both_ways(universe, tmp_path, monkeypatch, W):
    queued = _engine_day(universe, tmp_path / "queued", monkeypatch, W, True)
    direct = _engine_day(universe, tmp_path / "direct", monkeypatch, W, False)
    assert queued["trades"] and queued["hist"] >= 6 and "reset" in queued["gold_events"]
    assert queued == direct


def test_same_text_again_is_not_rewritten_until_forget(tmp_path, W):
    p = tmp_path / "b" / "NIFTY_state.json"
    for _ in range(50):                                         # the rules re-save an unchanged state per tick
        W.replace(p, "A")
    assert W.flush(5) and p.read_text() == "A" and W.stats["replaces"] == 1      # 50 saves, one write
    for _ in range(50):
        W.replace(p, "A")
    assert W.flush(5) and W.stats["replaces"] == 1 and W.stats["same"] == 50      # already on disk: no write
    W.replace(p, "B")
    W.replace(p, "A")                                           # back to A while B is still waiting: A wins
    assert W.flush(5) and p.read_text() == "A"
    p.unlink()                                                  # a reset moved the file away
    W.forget()
    W.replace(p, "A")
    assert W.flush(5) and p.read_text() == "A"


def test_list_days_is_shared_briefly_and_per_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DATA", tmp_path / "one")
    (tmp_path / "one" / "books" / "2026-11-02").mkdir(parents=True)
    (tmp_path / "one" / "books" / "2026-11-02" / "x.json").write_text("{}")
    assert days.list_days() == ["2026-11-02"]
    got = days.list_days()
    got.append("junk")                                          # a caller's list is its own
    assert days.list_days() == ["2026-11-02"]
    monkeypatch.setattr(C, "DATA", tmp_path / "two")            # another data folder: never the old answer
    assert days.list_days() == []


def test_gc_guard_keeps_full_collections_short():
    import gc
    keep = [{"t": float(i), "sp": [0.1, 0.2, None]} for i in range(400000)]     # a morning of chart samples
    t0 = time.perf_counter()
    gc.collect()
    slow = time.perf_counter() - t0
    try:
        freeze = diag.gc_guard(start=False)
        before = diag.GC["full"]
        t0 = time.perf_counter()
        gc.collect()
        fast = time.perf_counter() - t0
        assert diag.GC["full"] == before + 1 and diag.GC["frozen"] >= 800000
        assert fast < slow / 5                                    # the frozen samples are not looked at again
        keep.extend({"t": float(i), "sp": [0.3]} for i in range(1000))
        freeze()
        assert diag.GC["freezes"] >= 2 and len(keep) == 401000 and keep[-1]["sp"] == [0.3]     # data untouched
        t0 = time.perf_counter()
        for _ in range(200):
            freeze()
        assert time.perf_counter() - t0 < 0.5                     # a freeze costs next to nothing
    finally:
        gc.unfreeze()
        gc.callbacks.remove(diag._gc_cb)
