r"""
cepe_imbalance/test_split.py — the 17-Nov separate LIVE engine, offline.

  paper process never builds a live book · live process builds ONLY live books · live feed subscribes only the
  armed / holding index · one live index armed at a time · paper process forwards *_LIVE requests and marks the
  live rows "engine down" when the live engine cannot be reached · live settings in their own file

    .venv\Scripts\python.exe -m pytest live_trading_options/cepe_imbalance/test_split.py -q
"""
import json

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import engine as EN
from live_trading_options.cepe_imbalance import service as SV
from live_trading_options.cepe_imbalance.engine import Engine
from live_trading_options.cepe_imbalance.market import Market
from live_trading_options.cepe_imbalance.test_live import FakeKotak, LV, _MemControl, universe  # noqa: F401


class _Feed:
    def __init__(self):
        self.keys = set()

    def want(self, keys):
        new = set(keys) - self.keys
        self.keys |= set(keys)
        return len(new)

    def status(self):
        return {}


def live_ctx(universe):
    acct = LV.Account(client=FakeKotak(universe), start=False)
    return LV.LiveCtx(acct, _MemControl(), sleep=lambda s: None)


def test_paper_role_has_no_live_books(universe):
    eng = Engine(universe, Market(universe), persist=False, live=live_ctx(universe), role="paper")
    assert eng.runners and not any(C.is_live(n) for n in eng.runners)


def test_live_role_has_only_live_books(universe):
    eng = Engine(universe, Market(universe), persist=False, live=live_ctx(universe), role="live")
    assert set(eng.runners) == {"NIFTY_LIVE", "SENSEX_LIVE"}
    assert eng.quotes is None                                  # never writes the paper engine's tick files


def test_book_in_role():
    assert C.book_in_role("NIFTY", "paper") and not C.book_in_role("NIFTY_LIVE", "paper")
    assert C.book_in_role("NIFTY_LIVE", "live") and not C.book_in_role("NIFTY", "live")
    assert C.book_in_role("NIFTY", "all") and C.book_in_role("NIFTY_LIVE", "all")
    assert C.port_for("live") == C.LIVE_PORT != C.port_for("paper") == C.PORT


def test_live_feed_only_for_the_armed_index(universe):
    ctx = live_ctx(universe)
    m = Market(universe)
    eng = Engine(universe, m, persist=False, live=ctx, role="live")
    eng.feed = _Feed()
    eng.refresh_subscriptions()
    assert not eng.feed.keys                                   # nothing armed: no prices subscribed at all
    ctx.control.set("NIFTY_LIVE", True)
    eng.refresh_subscriptions()
    nifty = universe.underlyings["NIFTY"].spot_key
    sensex = universe.underlyings["SENSEX"].spot_key
    assert nifty in eng.feed.keys and sensex not in eng.feed.keys


def _svc(monkeypatch, universe, role):
    ctx = live_ctx(universe)
    eng = Engine(universe, Market(universe), persist=False, live=ctx, role=role)
    eng.feed = _Feed()
    monkeypatch.setattr(SV, "E", eng)
    monkeypatch.setattr(C, "ROLE", role)
    SV._forget()
    return eng, ctx


def test_only_one_live_index_armed(monkeypatch, universe):
    eng, ctx = _svc(monkeypatch, universe, "live")
    assert SV.live_arm("NIFTY_LIVE", {"armed": True, "confirm": "LIVE"})["ok"]
    r = SV.live_arm("SENSEX_LIVE", {"armed": True, "confirm": "LIVE"})
    body = json.loads(r.body)
    assert not body["ok"] and "NIFTY_LIVE is armed" in body["error"]
    assert not ctx.control.armed("SENSEX_LIVE")
    assert SV.live_arm("NIFTY_LIVE", {"armed": False})["ok"]
    assert SV.live_arm("SENSEX_LIVE", {"armed": True, "confirm": "LIVE"})["ok"]


def test_arm_subscribes_that_index(monkeypatch, universe):
    eng, ctx = _svc(monkeypatch, universe, "live")
    assert not eng.feed.keys
    SV.live_arm("SENSEX_LIVE", {"armed": True, "confirm": "LIVE"})
    assert universe.underlyings["SENSEX"].spot_key in eng.feed.keys


def test_paper_forwards_live_paths():
    assert SV._is_live_path("/live/NIFTY_LIVE/arm") and SV._is_live_path("/control/sensex_live")
    assert SV._is_live_path("/history/NIFTY_LIVE") and not SV._is_live_path("/control/NIFTY")
    assert not SV._is_live_path("/state") and not SV._is_live_path("/live")


def test_paper_state_adds_live_rows_and_marks_engine_down(monkeypatch, universe):
    _svc(monkeypatch, universe, "paper")
    row = {"name": "NIFTY_LIVE", "live": {"armed": True}, "blocked": None}
    monkeypatch.setattr(SV, "_live_call", lambda *a, **k: (200, json.dumps({"underlyings": [row]}).encode(), "x"))
    SV._live_last.update(state=None, at=0.0, error="")
    names = [x["name"] for x in SV._state()["underlyings"]]
    assert "NIFTY_LIVE" in names and "NIFTY" in names
    SV._forget()
    monkeypatch.setattr(SV, "_live_call", lambda *a, **k: (503, b'{"ok":false,"error":"live engine not reachable"}', "x"))
    rows = [x for x in SV._state()["underlyings"] if x["name"] == "NIFTY_LIVE"]
    assert rows and rows[0]["live"]["engine_down"] and "NOT REACHABLE" in rows[0]["blocked"]
    assert rows[0]["live"]["armed"] is True                    # last known state still shown


def test_live_settings_own_file(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DATA", tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({"NIFTY": {"params": {"lots": 3}},
                                                         "NIFTY_LIVE": {"params": {"lots": 2}}}))
    assert EN.load_settings("NIFTY_LIVE")["NIFTY_LIVE"]["params"]["lots"] == 2     # old values carried over
    EN.save_settings("NIFTY_LIVE", params={"lots": 4})
    live = json.loads((tmp_path / "settings_live.json").read_text())
    assert live == {"NIFTY_LIVE": {"params": {"lots": 4}}}                          # only live books in it
    assert json.loads((tmp_path / "settings.json").read_text())["NIFTY_LIVE"]["params"]["lots"] == 2
    EN.save_settings("NIFTY", params={"lots": 5})
    assert json.loads((tmp_path / "settings.json").read_text())["NIFTY"]["params"]["lots"] == 5



def _asgi(app, method, path, body=b""):
    """Call the ASGI app directly (no HTTP client needed) -> (status, body bytes)."""
    import asyncio
    p, _, qs = path.partition("?")
    scope = {"type": "http", "method": method, "path": p, "raw_path": p.encode(), "query_string": qs.encode(),
             "headers": [(b"content-type", b"application/json")], "http_version": "1.1", "scheme": "http",
             "server": ("t", 80), "client": ("t", 1), "root_path": ""}
    out = {"status": None, "body": b""}
    sent = [False]

    async def receive():
        if sent[0]:
            return {"type": "http.disconnect"}
        sent[0] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")
    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"]


def test_middleware_forwards_live_requests_with_body(monkeypatch, universe):
    _svc(monkeypatch, universe, "paper")
    seen = []

    def fake(method, path, body=None, timeout=5.0):
        seen.append((method, path, body))
        return 200, b'{"ok": true, "from": "live"}', "application/json"
    monkeypatch.setattr(SV, "_live_call", fake)
    st, b = _asgi(SV.app, "POST", "/live/NIFTY_LIVE/arm", b'{"armed": true, "confirm": "LIVE"}')
    assert st == 200 and json.loads(b)["from"] == "live"
    st, b = _asgi(SV.app, "GET", "/history/NIFTY_LIVE?max_points=10")
    assert json.loads(b)["from"] == "live"
    assert seen[0][0] == "POST" and seen[0][1] == "/live/NIFTY_LIVE/arm" and b'"confirm"' in seen[0][2]
    assert seen[1] == ("GET", "/history/NIFTY_LIVE?max_points=10", b"")
    st, b = _asgi(SV.app, "GET", "/positions/NIFTY")
    assert st == 200 and len(seen) == 2                       # paper books stay in this process


def test_live_entry_pairs_and_resize_with_real_orders(universe):
    from live_trading_options.cepe_imbalance.test_live import B, Rig as LiveRig
    g = LiveRig(universe)
    g.r.params["lots"] = 2
    g.eng.start(B)
    places = [kw for t, kw in g.k.calls if t == "place"]
    kinds = [kw["trading_symbol"][-2:] for kw in places]
    assert kinds[:2] == ["CE", "PE"] and kinds[2:4] == ["CE", "PE"]          # pairs, never all calls first
    assert g.open_lots() == 12
    n = len(places)
    res = g.eng.set_params(B, {"lots": 1})
    assert "2 -> 1" in res["resize"]
    g.move(g.f)
    new = [kw for t, kw in g.k.calls if t == "place"][n:]
    assert new and {kw["transaction_type"] for kw in new} == {"B"}           # cut = buy-backs only
    assert g.open_lots() == 6
    net = g.broker_net()
    assert sum(-v for v in net.values()) == 6 * 65                          # Kotak holds exactly the book
