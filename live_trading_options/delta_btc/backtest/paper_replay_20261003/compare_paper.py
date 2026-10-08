import os, sys, json, glob
os.environ["BTC_GUARD"] = "off"
os.environ["BTC_SPREAD"] = "median"
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"G:\fyers_data_pipeline\live_trading_options\delta_btc\backtest")
import pandas as pd
import vwap_history as H

SCR = os.path.dirname(os.path.abspath(__file__))
rows, tl = [], []
for f in sorted(glob.glob(os.path.join(SCR, "ist", "2026-*.json"))):
    p = json.load(open(f))
    day = p["date"]
    df = H.load_day(day)
    if df is None:
        print(day, "no history"); continue
    pair = p["pair"]
    first = p["candles"][0]["t"]
    forced = H.run_cycle("ist_day", day, df, force={"ce": pair["ce"], "pe": pair["pe"],
                                                    "first_candle": first, "combined": pair["combined"]})
    free = H.run_cycle("ist_day", day, df)
    # candle agreement (forced run builds candles on the same strikes)
    hc = {c["t"]: c for c in forced.get("candle_list", [])}
    cd, vd, side = [], [], []
    for c in p["candles"]:
        h = hc.get(c["t"])
        if not h or not c.get("vwap") or not h.get("vwap"):
            continue
        cd.append(abs(h["c"] - c["c"]) / max(c["c"], 1) * 100)
        vd.append(abs(h["vwap"] - c["vwap"]) / max(c["vwap"], 1) * 100)
        side.append((h["c"] > h["vwap"]) == (c["c"] > c["vwap"]))
    pn = sum(t["net_usd"] for t in p["trades"])
    rows.append({"date": day,
                 "paper_pair": f"{pair['ce']:.0f}/{pair['pe']:.0f}",
                 "bt_pair": f"{free.get('ce', 0):.0f}/{free.get('pe', 0):.0f}" if not free.get("skip") else free["skip"],
                 "close%": round(pd.Series(cd).median(), 2) if cd else None,
                 "vwap%": round(pd.Series(vd).median(), 2) if vd else None,
                 "same_side%": round(100 * sum(side) / len(side), 0) if side else None,
                 "paper_n": len(p["trades"]), "forced_n": len(forced.get("trades", [])),
                 "free_n": len(free.get("trades", [])),
                 "paper": round(pn, 2), "forced": round(forced.get("net", 0), 2),
                 "free": round(free.get("net", 0), 2)})
    for t in p["trades"]:
        tl.append((day, "paper", t["entry_time"][11:16], t["exit_time"][11:16], t["entry_combined"], t["exit_combined"], round(t["net_usd"], 1)))
    for t in forced.get("trades", []):
        tl.append((day, "forced", t["entry_time"][11:16], t["exit_time"][11:16], round(t["entry_combined"], 1), round(t["exit_combined"], 1), round(t["net_usd"], 1)))

r = pd.DataFrame(rows).set_index("date")
pd.set_option("display.width", 250)
print(r.to_string())
print("\nTOTAL  paper", round(r.paper.sum(), 2), " forced", round(r.forced.sum(), 2), " free", round(r.free.sum(), 2))
print("trades paper", r.paper_n.sum(), " forced", r.forced_n.sum(), " free", r.free_n.sum())
print("same strikes picked:", (r.paper_pair == r.bt_pair).sum(), "/", len(r))
pd.DataFrame(tl, columns=["date", "src", "entry", "exit", "in", "out", "net"]).to_csv(os.path.join(SCR, "trade_compare.csv"), index=False)
