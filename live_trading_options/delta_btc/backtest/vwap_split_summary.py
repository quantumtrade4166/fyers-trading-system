"""Summarise backtest/vwap_split_2026.py: totals, win days, worst day, max drawdown, by month."""
import sys
import json
from pathlib import Path
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
F = Path(__file__).resolve().parents[1] / "data" / "vwap_results" / "backtest" / "vwap_split_2026.json"
d = json.loads(F.read_text(encoding="utf-8"))
ok = {k: v for k, v in d.items() if not v.get("skip")}
print(f"days: {len(d)} total, {len(ok)} traded/run, {len(d) - len(ok)} skipped")
skips = pd.Series([v["skip"] for v in d.values() if v.get("skip")]).value_counts()
print(skips.to_string(), "\n")

df = pd.DataFrame(ok).T.drop(columns=["trades", "baseline_net"]).astype(float)
df.index = pd.to_datetime(df.index)
df = df.sort_index()

rows = []
for col in df.columns:
    s = df[col]
    eq = s.cumsum()
    rows.append({"variant": col, "total": s.sum(), "win%": (s > 0).mean() * 100,
                 "avg/day": s.mean(), "worst": s.min(), "best": s.max(),
                 "maxDD": (eq - eq.cummax()).min()})
r = pd.DataFrame(rows).set_index("variant")
print(r.round(2).sort_values("total", ascending=False).to_string(), "\n")

key = ["both|3|stop", "C|3|stop", "D|3|stop", "both|4|stop", "C|4|stop", "D|4|stop",
       "both|4|nostop", "D|4|nostop"]
print(df[key].groupby(df.index.to_period("M")).sum().round(1).to_string())
