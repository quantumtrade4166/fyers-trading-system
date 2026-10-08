import sys, time, datetime as dt, requests
sys.path.insert(0, r"G:\fyers_data_pipeline\Bitcoin options data")
sys.stdout.reconfigure(encoding="utf-8")
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import download_delta_btc_options as D
S = requests.Session()
def q(sym, a, b):
    for i in range(5):
        r = S.get(D.BASE + "/history/candles", params={"symbol": sym, "resolution": "1m", "start": a, "end": b}, timeout=30)
        if r.status_code == 429: time.sleep(2 + 2 * i); continue
        if r.status_code != 200: return []
        return r.json().get("result") or []
    return []
def one(w, a, b):
    mk = {c["time"]: c for c in q("MARK:" + w["symbol"], a, b)}
    if not mk: return []
    tr = {c["time"]: c for c in q(w["symbol"], a, b)}
    return [{"time": t, "symbol": w["symbol"], "opt_type": w["opt_type"], "strike": w["strike"], "contract_value": 0.001,
             "mark_open": m.get("open"), "mark_high": m.get("high"), "mark_low": m.get("low"), "mark": m.get("close"),
             "open": tr.get(t, {}).get("open"), "high": tr.get(t, {}).get("high"), "low": tr.get(t, {}).get("low"),
             "close": tr.get(t, {}).get("close"), "volume": tr.get(t, {}).get("volume")} for t, m in sorted(mk.items())]
for d in pd.date_range("2026-09-16", "2026-10-02").strftime("%Y-%m-%d"):
    f = D.OUT / "raw" / f"{d}.parquet"
    r = pd.read_parquet(f)
    sp = pd.read_parquet(D.OUT / "spot" / f"{d}.parquet")["close"]
    lo, hi = int(sp.min() * 0.94 // 100 * 100), int(sp.max() * 1.06 // 100 * 100)
    code = dt.date.fromisoformat(d).strftime("%d%m%y")
    have = set(r.symbol)
    want = [{"symbol": f"{'C' if t=='CE' else 'P'}-BTC-{k}-{code}", "opt_type": t, "strike": float(k)}
            for k in range(lo, hi + 100, 100) for t in ("CE", "PE")]
    miss = [w for w in want if w["symbol"] not in have]
    end = int(dt.datetime.fromisoformat(d + "T12:00:00+00:00").timestamp()); start = end - 86400
    with ThreadPoolExecutor(6) as ex:
        parts = list(ex.map(lambda w: one(w, start, end), miss))
    add = pd.DataFrame([x for p in parts for x in p])
    if not add.empty:
        add["time_ist"] = pd.to_datetime(add["time"], unit="s") + D.IST
        add["expiry"] = code
        r = pd.concat([r, add], ignore_index=True)
        tmp = f.with_suffix(".tmp"); r.to_parquet(tmp, index=False); tmp.replace(f)
    print(d, "had", len(have), "probed", len(miss), "added", add.symbol.nunique() if not add.empty else 0, flush=True)
