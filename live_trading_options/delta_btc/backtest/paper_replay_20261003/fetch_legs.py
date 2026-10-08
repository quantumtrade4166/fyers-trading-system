import sys, json, glob, time, datetime as dt, requests, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
BASE="https://api.india.delta.exchange/v2"; S=requests.Session()
def get(sym,a,b):
    for i in range(8):
        try:
            r=S.get(BASE+"/history/candles",params={"symbol":sym,"resolution":"1m","start":a,"end":b},timeout=30)
            if r.status_code==429: time.sleep(2+3*i); continue
            r.raise_for_status(); return r.json().get("result") or []
        except Exception as e: time.sleep(2+3*i)
    raise RuntimeError(sym)
rows=[]
for f in sorted(glob.glob("ist/2026-*.json")):
    d=json.load(open(f)); day=dt.date.fromisoformat(d["date"])
    a=int(dt.datetime(day.year,day.month,day.day,3,0,tzinfo=dt.timezone.utc).timestamp())
    b=int(dt.datetime(day.year,day.month,day.day,12,0,tzinfo=dt.timezone.utc).timestamp())
    for leg in ("ce","pe"):
        sym=d["pair"][leg+"_symbol"]
        mk={c["time"]:c for c in get("MARK:"+sym,a,b)}; tr={c["time"]:c for c in get(sym,a,b)}
        for t,c in mk.items():
            x=tr.get(t,{})
            rows.append(dict(date=d["date"],leg=leg,symbol=sym,time=t,mo=c["open"],mh=c["high"],ml=c["low"],mc=c["close"],vol=x.get("volume",0) or 0))
        print(d["date"],sym,len(mk),len(tr),flush=True)
df=pd.DataFrame(rows); df["t"]=pd.to_datetime(df.time,unit="s")+pd.Timedelta(hours=5,minutes=30)
df.to_parquet("legs.parquet"); print(len(df))
