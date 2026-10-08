import sys, time, requests, datetime as dt
sys.stdout.reconfigure(encoding="utf-8")
BE = "https://eapi.binance.com/eapi/v1"; DE = "https://api.india.delta.exchange/v2"
info = requests.get(BE + "/exchangeInfo", timeout=30).json()
spot = float(requests.get(BE + "/index", params={"underlying": "BTCUSDT"}, timeout=30).json()["indexPrice"])
# nearest common expiry (Delta settles 12:00 UTC daily; Binance 08:00 UTC) -> use tomorrow for both
exp = (dt.datetime.utcnow() + dt.timedelta(days=1)).date()
bcode, dcode = exp.strftime("%y%m%d"), exp.strftime("%d%m%y")
bsyms = {s["symbol"] for s in info["optionSymbols"] if s["symbol"].startswith(f"BTC-{bcode}-")}
dprod = requests.get(DE + "/tickers", params={"contract_types": "call_options,put_options", "underlying_asset_symbols": "BTC"}, timeout=30).json()["result"]
dsyms = {p["symbol"] for p in dprod if p["symbol"].endswith(dcode)}
def walk(levels, qty):  # levels [(price, size_btc)], fill qty BTC -> avg price
    got = cost = 0
    for p, s in levels:
        take = min(s, qty - got); got += take; cost += take * p
        if got >= qty - 1e-9: return cost / got
    return None
def bin_book(sym):
    b = requests.get(BE + "/depth", params={"symbol": sym, "limit": 50}, timeout=30).json()
    f = lambda L: [(float(p), float(q)) for p, q in L]
    return f(b["bids"]), f(b["asks"])
def del_book(sym):
    b = requests.get(DE + f"/l2orderbook/{sym}", params={"depth": 50}, timeout=30).json()["result"]
    f = lambda L: [(float(x["price"]), float(x["size"]) * 0.001) for x in L]
    return f(b["buy"]), f(b["sell"])
atm = round(spot / 500) * 500
print(f"BTC {spot:.0f}  expiry {exp}  (Binance 08:00 UTC, Delta 12:00 UTC)\n")
print(f"{'option':12}{'exch':9}{'bid':>8}{'ask':>8}{'spread':>8}{'bid BTC':>9}{'ask BTC':>9}{'sell 1BTC':>10}{'buy 1BTC':>10}")
for k in range(atm - 3000, atm + 3001, 1000):
    for t in ("C", "P"):
        if (t == "C" and k < atm) or (t == "P" and k > atm): continue
        for ex, sym, fn in (("Binance", f"BTC-{bcode}-{k}-{t}", bin_book), ("Delta", f"{t}-BTC-{k}-{dcode}", del_book)):
            if sym not in (bsyms if ex == "Binance" else dsyms):
                print(f"{k}{t:<7}{ex:9}  not listed"); continue
            bids, asks = fn(sym)
            bb = bids[0][0] if bids else 0; ba = asks[0][0] if asks else 0
            s1, b1 = walk(bids, 1.0), walk(asks, 1.0)
            print(f"{k}{t:<7}{ex:9}{bb:8.1f}{ba:8.1f}{ba-bb:8.1f}{sum(q for _,q in bids):9.2f}{sum(q for _,q in asks):9.2f}"
                  f"{(f'{s1:.1f}' if s1 else 'no fill'):>10}{(f'{b1:.1f}' if b1 else 'no fill'):>10}")
