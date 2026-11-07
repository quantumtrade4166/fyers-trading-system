"""1-year in-sample / 1-year out-of-sample test of Nifty ST Directional.
Settings grid: stop-to-cost (off / 25) x CPR max width (30..100 / off).
Pick on year N only, then score that pick on year N+1. 10 lots, 0.5 pt slip, net."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import pandas as pd

R = Path(__file__).parent / "results"
OUT = R / "IS_OOS"; OUT.mkdir(exist_ok=True)
cpr = pd.read_csv(R / "cpr_width_by_day.csv", parse_dates=["d"]).set_index("d")["cpr_width"]

def daily(year, be):
    tag = f"Y{year}_10LOTS{'_BE25' if be else ''}_SLIP05"
    s = pd.read_csv(R / tag / "daily_pnl.csv", parse_dates=["date"]).set_index("date")["net_pnl"]
    return s

def score(s):
    eq = s.cumsum(); dd = (eq - eq.cummax()).min()
    net = s.sum()
    return dict(net=net, max_dd=dd, ret_dd=net / abs(dd) if dd < 0 else float("inf"),
                days_traded=int((s != 0).sum()))

CPRS = [30, 40, 50, 60, 70, 80, 90, 100, None]
rows = []
for y in (2021, 2022, 2023, 2024, 2025, 2026):
    for be in (False, True):
        s = daily(y, be)
        w = cpr.reindex(s.index)
        for c in CPRS:
            f = s if c is None else s.where(w <= c, 0.0)   # unknown width -> skip day
            rows.append(dict(year=y, be25=be, cpr_max=c or "off", **score(f)))
grid = pd.DataFrame(rows)
grid.to_csv(OUT / "grid.csv", index=False)

pairs = [(2021, 2022), (2022, 2023), (2023, 2024), (2024, 2025), (2025, 2026)]
wf = []
for ins, oos in pairs:
    for crit in ("net", "ret_dd"):
        g = grid[grid.year == ins].sort_values(crit, ascending=False).iloc[0]
        o = grid[(grid.year == oos) & (grid.be25 == g.be25) & (grid.cpr_max == g.cpr_max)].iloc[0]
        best_o = grid[grid.year == oos].sort_values(crit, ascending=False).iloc[0]
        live = grid[(grid.year == oos) & (grid.be25) & (grid.cpr_max == 60)].iloc[0]
        base = grid[(grid.year == oos) & (~grid.be25) & (grid.cpr_max == "off")].iloc[0]
        wf.append(dict(in_year=ins, out_year=oos, picked_by=crit,
                       pick=f"BE25={'on' if g.be25 else 'off'} CPR<={g.cpr_max}",
                       in_net=g.net, in_dd=g.max_dd,
                       out_net=o.net, out_dd=o.max_dd,
                       out_best_possible=best_o.net, out_live_setting=live.net, out_plain_rules=base.net))
wf = pd.DataFrame(wf)
wf.to_csv(OUT / "walk_forward.csv", index=False)
pd.set_option("display.width", 250)
print(grid.assign(net=(grid.net/1e5).round(2), max_dd=(grid.max_dd/1e5).round(2), ret_dd=grid.ret_dd.round(2)).to_string(index=False))
print()
num = [c for c in wf.columns if c.startswith(("in_n","in_d","out_n","out_d","out_b","out_l","out_p"))]
print(wf.assign(**{c: (wf[c]/1e5).round(2) for c in num}).to_string(index=False))
