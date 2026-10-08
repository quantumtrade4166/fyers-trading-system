import sys, time, datetime as dt
sys.path.insert(0, r"G:\fyers_data_pipeline\Bitcoin options data")
sys.stdout.reconfigure(encoding="utf-8")
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import download_delta_btc_options as D
LO, HI = "2026-09-16", "2026-10-02"
rows, after = [], None
while True:
    p = {"contract_types": "call_options,put_options", "states": "expired", "page_size": 500}
    if after: p["after"] = after
    r = D.get("/products", p)
    for x in r["result"]:
        if x["underlying_asset"]["symbol"] != "BTC": continue
        rows.append({"symbol": x["symbol"], "opt_type": "CE" if x["contract_type"] == "call_options" else "PE",
                     "strike": float(x["strike_price"]), "settlement_time": x["settlement_time"],
                     "contract_value": float(x["contract_value"])})
    after = r["meta"].get("after")
    oldest = min(x["settlement_time"] for x in r["result"]) if r["result"] else ""
    print(f"  listed {len(rows)} BTC, oldest {oldest}", flush=True)
    if not after or not r["result"] or oldest[:10] < LO: break
prods = pd.DataFrame(rows).drop_duplicates("symbol")
prods["settle"] = pd.to_datetime(prods["settlement_time"], utc=True)
days = sorted(d for d in prods["settle"].dt.strftime("%Y-%m-%d").unique() if LO <= d <= HI)
for day in days:
    f = D.OUT / "raw" / f"{day}.parquet"
    if f.exists(): continue
    g = prods[prods["settle"].dt.strftime("%Y-%m-%d") == day]
    end = int(g["settle"].max().timestamp()); start = end - 86400
    t0 = time.time()
    with ThreadPoolExecutor(6) as ex:
        parts = list(ex.map(lambda r: D.one_contract(r, start, end), g.to_dict("records")))
    df = pd.DataFrame([x for p in parts for x in p])
    spot = D.candles(".DEXBTUSD", start, end)
    if spot:
        sp = pd.DataFrame(spot); sp["time_ist"] = pd.to_datetime(sp["time"], unit="s") + D.IST
        sp.to_parquet(D.OUT / "spot" / f"{day}.parquet", index=False)
    if not df.empty:
        df["time_ist"] = pd.to_datetime(df["time"], unit="s") + D.IST
        df["expiry"] = dt.datetime.strptime(day, "%Y-%m-%d").strftime("%d%m%y")
    tmp = f.with_suffix(".tmp"); df.to_parquet(tmp, index=False); tmp.replace(f)
    print(f"  {day}: {len(g)} contracts, {len(df):,} rows ({time.time()-t0:.0f}s)", flush=True)
print("done")
