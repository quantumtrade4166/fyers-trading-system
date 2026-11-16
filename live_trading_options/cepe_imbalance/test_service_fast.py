"""cepe_imbalance/test_service_fast.py — the read-side speed-ups of 16-Nov-2026 give the SAME answers.

Shared answers (_cached), running day extremes (_day_stats), history `since` by bisect and the cached
book read are display-only shortcuts; every test compares them with the plain slow way."""
import json
import random
import threading
import time
import types

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import days
from live_trading_options.cepe_imbalance import service as S


def _rows(n, t0=1000.0, seed=1):
    rnd = random.Random(seed)
    return [{"t": round(t0 + i * 0.5, 2), "mtm": round(rnd.uniform(-5000, 5000), 2),
             "imb": round(rnd.uniform(-40, 40), 2)} for i in range(n)]


def _slow(h):
    m = [x["mtm"] for x in h if x.get("mtm") is not None]
    i = [x["imb"] for x in h if x.get("imb") is not None]
    return {"last_mtm": h[-1]["mtm"] if h else None, "hi": max(m) if m else None, "lo": min(m) if m else None,
            "imx": max(i) if i else None, "imn": min(i) if i else None}


class _Book:
    def __init__(self, total=0.0, trades=()):
        self.positions, self.trades, self._total = {}, list(trades), total

    def mtm(self):
        return {"total": self._total, "realized": 0.0}


def _engine(monkeypatch, hist):
    runners = {n: types.SimpleNamespace(date="2099-01-01", book=_Book(12.5, [{"time": "10:00:00.000"}]),
                                        running=True, state={"phase": "run"}) for n in hist}
    e = types.SimpleNamespace(runners=runners, hist=hist)
    monkeypatch.setattr(S, "E", e)
    S._day_run.clear()
    S._forget()
    return e


def test_day_stats_follow_a_growing_list(monkeypatch):
    h = []
    _engine(monkeypatch, {"NIFTY": h})
    assert S._day_stats("NIFTY") == _slow(h)                   # empty
    src = _rows(5000)
    for i in range(0, 5000, 137):                              # called again and again as samples arrive
        h.extend(src[i:i + 137])
        assert S._day_stats("NIFTY") == _slow(h)


def test_day_stats_survive_the_trim_and_restart_on_a_new_list(monkeypatch):
    h = _rows(3000)
    e = _engine(monkeypatch, {"NIFTY": h})
    whole = _slow(h)
    assert S._day_stats("NIFTY") == whole
    del h[:-500]                                               # engine trims the oldest samples in place
    h.extend(_rows(10, t0=h[-1]["t"] + 0.5, seed=7))
    got = S._day_stats("NIFTY")
    assert got["last_mtm"] == h[-1]["mtm"]
    assert got["hi"] >= whole["hi"] and got["lo"] <= whole["lo"]     # the day's extremes are kept
    e.hist["NIFTY"] = _rows(40, seed=3)                        # reset / new day: a brand-new list
    assert S._day_stats("NIFTY") == _slow(e.hist["NIFTY"])


def test_day_table_same_as_the_slow_way(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DATA", tmp_path)
    hist = {"NIFTY": _rows(4000, seed=2), "GOLD": _rows(900, seed=5), "BTC": []}
    e = _engine(monkeypatch, hist)
    fast = S._day("live")
    for row in fast["rows"]:
        nm = row["name"]
        want = days.summary("2099-01-01", nm, hist=list(e.hist[nm]))
        for k in ("mtm_high", "mtm_low", "imb_max", "imb_min", "adjustments", "atm_rule", "a", "b", "first", "last"):
            assert row[k] == want[k], (nm, k)
        assert row["pnl"] == 12.5 and row["running"] is True and row["fills"] == 1      # today: live numbers
    assert fast["total"] == round(12.5 * 2, 2) and fast["total_usd"] == 12.5


def test_history_since_same_as_filtering(monkeypatch):
    h = _rows(6000)
    _engine(monkeypatch, {"NIFTY": h})
    for since in (0.5, h[0]["t"], h[1234]["t"], h[1234]["t"] + 0.01, h[-1]["t"], h[-1]["t"] + 99):
        got = json.loads(S.history("NIFTY", since=since).body)["rows"]
        assert got == [x for x in h if x.get("t", 0) > since]
    full = json.loads(S.history("NIFTY", max_points=800).body)["rows"]
    assert full == days.downsample(list(h), 800)


def test_cached_builds_once_for_many_callers_and_forgets_after_a_change():
    S._forget()
    calls, out = [], []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return len(calls)

    ts = [threading.Thread(target=lambda: out.append(S._cached("k", 5.0, slow))) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1 and out == [1] * 20
    assert S._cached("k", 5.0, slow) == 1                      # still shared
    S._forget()
    assert S._cached("k", 5.0, slow) == 2                      # built fresh after a control / order
    assert S._cached("k", 0.05, slow) == 2
    time.sleep(0.3)
    assert S._cached("k", 0.05, slow) == 2                     # 5 s answer above is still valid
    S._forget()


def test_an_answer_built_across_a_change_is_not_kept():
    S._forget()
    n = []

    def build():
        n.append(1)
        if len(n) == 1:
            S._forget()                                        # an order lands while this answer is being built
        return len(n)

    assert S._cached("x", 5.0, build) == 1                     # this caller gets it ...
    assert S._cached("x", 5.0, build) == 2                     # ... but it was not stored
    assert S._cached("x", 5.0, build) == 2
    S._forget()


def test_fast_falls_back_for_things_json_cannot_write():
    assert json.loads(S._fast({"a": 1, "b": [1.5, None, "x"]}).body) == {"a": 1, "b": [1.5, None, "x"]}
    odd = {"a": {1, 2}}
    assert S._fast(odd) is odd                                 # FastAPI's own encoder handles it, as before
    nan = {"a": float("nan")}
    assert S._fast(nan) is nan


def test_positions_cache_follows_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DATA", tmp_path)
    b = tmp_path / "books" / "2026-11-02"
    b.mkdir(parents=True)

    def put(realized, n=1):
        pos = {f"k{i}": {"key": f"k{i}", "kind": "CE", "strike": 9200.0 + i, "expiry": "2026-11-17",
                         "lots": 0, "realized": realized} for i in range(n)}
        (b / "GOLD.json").write_text(json.dumps({"positions": pos}), encoding="utf-8")

    assert days.positions("2026-11-02", "GOLD") == []
    put(-100.0)
    assert days.positions("2026-11-02", "GOLD")[0]["realized"] == -100.0
    first = days.positions("2026-11-02", "GOLD")
    first.clear()                                              # a caller's list is its own
    assert len(days.positions("2026-11-02", "GOLD")) == 1
    put(250.0, n=3)
    got = days.positions("2026-11-02", "GOLD")
    assert len(got) == 3 and got[0]["realized"] == 250.0


def _asgi(app, method, path, body=None):
    """One request straight into the ASGI app (no test client / network needed). Returns parsed JSON."""
    import asyncio
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"", "scheme": "http", "server": ("t", 80),
             "client": ("t", 1), "headers": [(b"content-type", b"application/json"),
                                              (b"content-length", str(len(raw)).encode())]}
    out = []

    async def run():
        sent = []

        async def receive():
            if sent:
                await asyncio.sleep(3600)
            sent.append(1)
            return {"type": "http.request", "body": raw, "more_body": False}

        async def send(m):
            if m["type"] == "http.response.body":
                out.append(m.get("body", b""))
        await app(scope, receive, send)
    asyncio.run(run())
    return json.loads(b"".join(out))


def test_http_state_is_shared_and_fresh_after_a_post(monkeypatch):
    n = {"summary": 0}

    def summary():
        n["summary"] += 1
        return {"now": "x", "underlyings": [{"name": "NIFTY", "n": n["summary"]}], "feed": None}

    e = types.SimpleNamespace(runners={"NIFTY": types.SimpleNamespace(date="2099-01-01")}, hist={"NIFTY": []},
                              summary=summary, set_auto=lambda nm, a, t: {"ok": True})
    monkeypatch.setattr(S, "E", e)
    S._forget()
    a = _asgi(S.app, "GET", "/state")
    b = _asgi(S.app, "GET", "/state")
    assert a == b and n["summary"] == 1                        # second viewer shares the first answer
    assert _asgi(S.app, "POST", "/auto/NIFTY", {"auto_start": True, "auto_time": "09:05"}) == {"ok": True}
    assert _asgi(S.app, "GET", "/state")["underlyings"][0]["n"] == 2       # fresh straight after a control
    time.sleep(0.5)
    assert _asgi(S.app, "GET", "/state")["underlyings"][0]["n"] == 3       # and the 0.4 s share runs out
    S._forget()
