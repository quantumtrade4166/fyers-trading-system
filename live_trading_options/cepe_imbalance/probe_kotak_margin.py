"""
cepe_imbalance/probe_kotak_margin.py — READ-ONLY look at Kotak P (= Kotak D, env *_D) margin fields.
====================================================================================================
Prints limits() (every field), the shape of positions(), and margin_required() for 1 lot of the
NIFTY / SENSEX ATM CE+PE of the nearest expiry. Never places, modifies or cancels an order.
Run on the VPS:  .venv\\Scripts\\python.exe -m live_trading_options.cepe_imbalance.probe_kotak_margin
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def short(x, n=1500):
    try:
        s = json.dumps(x, default=str)
    except Exception:
        s = repr(x)
    return s[:n]


def main():
    from live_trading_options.mcx_terminal import config as MC
    from live_trading_options.mcx_terminal import kotak_session
    from live_trading_options.cepe_imbalance import config as C
    from live_trading_options.cepe_imbalance import instruments

    MC.ENV_SUFFIX = C.ENV_SUFFIX
    s = kotak_session.login()
    cl = s.client
    for kw in ({}, {"segment": "FO", "exchange": "NSE", "product": "NRML"}, {"segment": "ALL"}):
        try:
            r = cl.limits(**kw)
        except Exception as e:
            r = {"exception": repr(e)}
        print(f"limits({kw}) ->", short(r, 4000))
    try:
        p = cl.positions()
        rows = p.get("data") if isinstance(p, dict) else p
        print("positions rows:", len(rows) if isinstance(rows, list) else short(p, 300))
        if isinstance(rows, list) and rows:
            print("positions row keys:", sorted(rows[0].keys()))
    except Exception as e:
        print("positions exception", repr(e))
    u = instruments.load(cl)
    for name in ("NIFTY", "SENSEX"):
        und = u.underlyings.get(name)
        if not und:
            print(name, "not in universe")
            continue
        e = und.expiries()[0]
        ks = und.strikes(e)
        q = cl.quotes(instrument_tokens=[{"instrument_token": und.spot_key.split("|")[1],
                                          "exchange_segment": und.spot_key.split("|")[0]}], quote_type="ltp")
        print(name, "expiry", e, "lot", und.lot, "spot quote", short(q, 300))
        try:
            spot = float((q[0] if isinstance(q, list) else q["data"][0]).get("ltp"))
        except Exception:
            spot = ks[len(ks) // 2]
        atm = min(ks, key=lambda k: abs(k - spot))
        for kind in ("CE", "PE"):
            c = und.chains[e][atm][kind]
            try:
                r = cl.margin_required(exchange_segment=c.seg, price="0", order_type="MKT", product="NRML",
                                       quantity=str(c.lot), instrument_token=str(c.token), transaction_type="S")
            except Exception as ex:
                r = {"exception": repr(ex)}
            print(f"margin_required SELL 1 lot {c.symbol} ->", short(r, 1500))


if __name__ == "__main__":
    main()
