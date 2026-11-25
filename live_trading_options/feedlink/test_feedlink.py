"""
feedlink tests - offline, no broker.   .venv\\Scripts\\python.exe -m live_trading_options.feedlink.test_feedlink
"""
import sys
import time
from types import SimpleNamespace as NS

sys.stdout.reconfigure(encoding="utf-8")

from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.market import Market
from live_trading_options.feedlink import kite_assist as KA
from live_trading_options.feedlink.kite_master import KiteMaster
from live_trading_options.feedlink.kotak_ticker import KotakTicker, Mapping

HDR = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
NFO = HDR + ("11001,1,NIFTY26DEC25900CE,NIFTY,0,2026-12-02,25900,0.05,65,CE,NFO-OPT,NFO\n"
             "11002,2,NIFTY26DEC25900PE,NIFTY,0,2026-12-02,25900,0.05,65,PE,NFO-OPT,NFO\n"
             "11003,3,NIFTY26DEC26000CE,NIFTY,0,2026-12-02,26000,0.05,65,CE,NFO-OPT,NFO\n"
             "11009,9,NIFTY26DECFUT,NIFTY,0,2026-12-30,0,0.05,65,FUT,NFO-FUT,NFO\n")
BFO = HDR + "22001,1,SENSEX26D0384000CE,SENSEX,0,2026-12-03,84000,0.05,20,CE,BFO-OPT,BFO\n"
MCX = HDR + "33001,1,CRUDEOIL26DEC5800CE,CRUDEOIL,0,2026-12-14,5800,0.1,100,CE,MCX-OPT,MCX\n"

OK = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    OK.append(bool(cond))


def kc(key, name, expiry, strike, kind):
    seg, _, tok = key.partition("|")
    return NS(key=key, seg=seg, token=int(tok), name=name, expiry=expiry, strike=strike, kind=kind)


CONTRACTS = [kc("nse_fo|501", "NIFTY", "2026-12-02", 25900.0, "CE"), kc("nse_fo|502", "NIFTY", "2026-12-02", 25900.0, "PE"),
             kc("nse_fo|503", "NIFTY", "2026-12-02", 26000.0, "CE"), kc("nse_fo|509", "NIFTY", "2026-12-30", 0.0, "FUT"),
             kc("bse_fo|601", "SENSEX", "2026-12-03", 84000.0, "CE")]


def master(ex=("NFO", "BFO")):
    return KiteMaster(ex, texts={"NFO": NFO, "BFO": BFO, "MCX": MCX})


class FakeFeed:
    def __init__(self):
        self.keys, self.last_tick_at = [], time.time()

    def want(self, keys):
        self.keys += [k for k in keys if k not in self.keys]
        return len(keys)

    def want_depth(self, toks):
        return 0

    def status(self):
        return {"authed": True}

    def start(self):
        pass

    def stop(self):
        pass


class FakeTicker:
    MODE_LTP, MODE_QUOTE, MODE_FULL = "ltp", "quote", "full"

    def __init__(self):
        self.subs, self.modes = [], {}
        self.on_ticks = self.on_connect = self.on_close = self.on_error = None

    def subscribe(self, toks):
        self.subs += list(toks)

    def set_mode(self, m, toks):
        for t in toks:
            self.modes[t] = m

    def connect(self, threaded=True):
        self.on_connect and self.on_connect(self, {})

    def close(self):
        pass

    def push(self, ticks):
        self.on_ticks(self, ticks)


def test_master():
    m = master(("NFO", "BFO", "MCX"))
    check("master: option by what it is", m.token("nse_fo", "NIFTY", "2026-12-02", 25900, "CE") == 11001)
    check("master: future", m.token("nse_fo", "NIFTY", "2026-12-30", 0, "FUT") == 11009)
    check("master: BSE + MCX", m.token("bse_fo", "SENSEX", "2026-12-03", 84000, "CE") == 22001
          and m.token("mcx_fo", "CRUDEOIL", "2026-12-14", 5800, "CE") == 33001)
    check("master: index by name", m.token("nse_cm", "NIFTY", "", 0, "IDX") == 256265
          and m.token("bse_cm", "SENSEX", "", 0, "IDX") == 265)
    check("master: unknown -> None", m.token("nse_fo", "NIFTY", "2026-12-02", 25950, "CE") is None)


def ticker():
    f = FakeFeed()
    kt = KotakTicker(tag="t", sessions=object(), mapping=Mapping(master(), CONTRACTS), feed=f, log_fn=lambda *a: None)
    got = []
    kt.on_ticks = lambda ws, t: got.extend(t)
    return kt, f, got


def test_kotak_ticker_subscribe_and_shape():
    kt, f, got = ticker()
    kt.subscribe([256265, 11001, 11002, 99999])
    check("ticker: Kite tokens -> Kotak keys (index by name)", f.keys == ["nse_cm|Nifty 50", "nse_fo|501", "nse_fo|502"])
    check("ticker: unknown token reported, not streamed", 99999 in kt._unknown)
    now = time.time()
    kt._on_kotak([Tick("nse_fo|501", 120.5, 120.0, 121.0, 650, 325, 1000, 5000, int(now), now),
                  Tick("nse_cm|Nifty 50", 25912.3, None, None, None, None, None, None, int(now), now)])
    o, s = got
    check("ticker: option tick keyed by the KITE token", o["instrument_token"] == 11001 and o["last_price"] == 120.5)
    check("ticker: bid / ask in Kite depth shape", o["depth"]["buy"][0]["price"] == 120.0 and o["depth"]["sell"][0]["price"] == 121.0)
    check("ticker: index tick, not tradable", s["instrument_token"] == 256265 and s["tradable"] is False)
    check("ticker: exchange_timestamp is a naive IST datetime", s["exchange_timestamp"].tzinfo is None)


def test_kotak_ticker_drops_replays():
    kt, f, got = ticker()
    kt.subscribe([11001])
    now = time.time()
    kt._on_kotak([Tick("nse_fo|501", 120.0, None, None, None, None, None, None, int(now), now)])
    kt._on_kotak([Tick("nse_fo|501", 99.0, None, None, None, None, None, None, int(now) - 23 * 60, now)])
    check("ticker: a 23-minute-old replay is dropped", len(got) == 1 and kt.dropped == 1)
    kt._on_kotak([Tick("nse_fo|501", 121.0, None, None, None, None, None, None, int(now) - 2, now)])
    check("ticker: a slightly older (2 s) real tick still passes", len(got) == 2)


def test_kotak_ticker_rest_keys():
    kt, f, got = ticker()
    kt.subscribe([256265, 11003, 11001, 11002])
    now = time.time()
    kt._on_kotak([Tick("nse_cm|Nifty 50", 25990.0, None, None, None, None, None, None, int(now), now)])
    ks = kt.rest_keys()
    check("ticker REST: index first, then nearest strikes", ks[0] == "nse_cm|Nifty 50" and ks[1] == "nse_fo|503")


def test_cepe_assist():
    by_key = {c.key: c for c in CONTRACTS}
    u = NS(by_key=by_key)
    mk = Market(NS(underlyings={}))
    out = []
    ft = FakeTicker()
    a = KA.KiteAssist(KA.cepe_resolver(u, master()), KA.cepe_emitter(mk, out.extend), cap=3, tag="t", ticker=ft)
    a.want(["nse_cm|Nifty 50", "nse_fo|501", "nse_fo|502", "nse_fo|503", "nse_fo|999"])
    check("assist: subscribes mapped Kite tokens in FULL mode, up to the cap",
          ft.subs == [256265, 11001, 11002] and ft.modes[11001] == "full" and a.over_cap == 1 and a.unknown == 1)
    a.start()
    import datetime as dt
    ts = dt.datetime.now().replace(microsecond=0)
    ft.push([{"instrument_token": 11001, "last_price": 101.5, "exchange_timestamp": ts,
              "depth": {"buy": [{"price": 101.0, "quantity": 650}], "sell": [{"price": 102.0, "quantity": 130}]}}])
    t = out[-1]
    check("assist: Kite tick -> cepe Tick on the Kotak key", t.key == "nse_fo|501" and t.ltp == 101.5 and t.bid == 101.0 and t.ask == 102.0)
    check("assist: alive after a tick", a.alive())
    mk.apply(out)
    n = len(out)
    ft.push([{"instrument_token": 11001, "last_price": 101.5, "exchange_timestamp": ts,
              "depth": {"buy": [{"price": 101.0, "quantity": 650}], "sell": [{"price": 102.0, "quantity": 130}]}}])
    check("assist: a price the engine already holds is not sent again", len(out) == n)


def test_desk_assist_and_dual():
    by_token = {501: kc("nse_fo|501", "NIFTY", "2026-12-02", 25900.0, "CE"),
                26000: NS(seg="nse_cm", token=26000, name="NIFTY", expiry="", strike=0.0, kind="IDX")}
    u = NS(by_token=by_token, products={})
    last, out = {}, []
    ft = FakeTicker()
    a = KA.KiteAssist(KA.desk_resolver(u, master()), KA.desk_emitter(last, out.extend), cap=10, tag="t", ticker=ft)
    f = FakeFeed()
    d = KA.DualFeed(f, a)
    d.want([501, 26000])
    check("dual: want goes to Kotak AND Kite", f.keys == [501, 26000] and ft.subs == [11001, 256265])
    d.start()
    import datetime as dt
    ft.push([{"instrument_token": 256265, "last_price": 25901.2, "exchange_timestamp": dt.datetime.now()}])
    t = out[-1]
    check("desk assist: 13-field tuple on the Kotak token", len(t) == 13 and t[0] == 26000 and t[1] == 25901.2 and t[10] > 0)
    check("dual: status carries the Kite part", "kite" in d.status() and d.status()["authed"])
    check("dual: other attributes reach the Kotak feed", d.last_tick_at == f.last_tick_at)


def test_bus_end_to_end():
    """Kite (fake) -> real TickBus -> BusTicker -> KiteAssist -> cepe Tick."""
    import socket
    from live_trading_options.tickbus.bus_client import BusTicker
    from live_trading_options.tickbus.bus_server import TickBus
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    kite = FakeTicker()
    bus = TickBus(kite, port=port, log=lambda *a: None)
    bus.start(threaded=True)
    try:
        u = NS(by_key={c.key: c for c in CONTRACTS})
        mk = Market(NS(underlyings={}))
        out = []
        a = KA.KiteAssist(KA.cepe_resolver(u, master()), KA.cepe_emitter(mk, out.extend), cap=50, tag="t",
                          ticker=BusTicker(host="127.0.0.1", port=port, log=lambda *a: None))
        a.want(["nse_fo|503"])
        a.start()
        end = time.time() + 3
        while time.time() < end and 11003 not in kite.subs:
            time.sleep(0.02)
        check("bus: the assist's token reaches the one real Kite ticker", 11003 in kite.subs)
        kite.push([{"instrument_token": 11003, "last_price": 77.0}])
        end = time.time() + 3
        while time.time() < end and not out:
            time.sleep(0.02)
        check("bus: Kite tick arrives as a cepe Tick", bool(out) and out[0].key == "nse_fo|503" and out[0].ltp == 77.0)
    finally:
        a.stop()
        bus.stop()


def test_kotak_down_falls_back_to_kite():
    kt = KotakTicker(tag="t", log_fn=lambda *a: None)
    kt.OPEN_RETRY_S = 0.01

    def boom():
        raise RuntimeError("Kotak login failed")
    kt._open = boom
    kite = FakeTicker()
    subs = []
    kt.on_connect = lambda ws, r: subs.append("engine on_connect")
    kt.subscribe([11001])                                  # asked for while Kotak was being tried
    kt.connect(threaded=True, fallback=lambda: kite)
    check("fallback: after 3 failed Kotak starts the engine runs on its own Kite ticker",
          kt.via_kite is kite and kite.subs == [11001] and subs == ["engine on_connect"])
    kt.subscribe([11002])
    check("fallback: later subscribes go straight to Kite", kite.subs == [11001, 11002])


if __name__ == "__main__":
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        fn()
    print(f"\n{sum(OK)}/{len(OK)} passed")
    sys.exit(0 if all(OK) else 1)
