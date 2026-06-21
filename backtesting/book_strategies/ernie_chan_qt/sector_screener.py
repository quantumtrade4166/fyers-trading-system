"""
sector_screener.py
Systematic sector-by-sector pairwise cointegration screener.
Yahoo Finance 2015-2024 only for screening.
All n*(n-1)/2 pairs per sector. Hard-skips known pairs.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
PROJECT_ROOT = Path(".").resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np
import yfinance as yf
from itertools import combinations
from statsmodels.tsa.stattools import adfuller
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

STOCK_DIR   = Path("backtesting/book_strategies/ernie_chan_qt/data/stocks")
RESULTS_DIR = Path("backtesting/book_strategies/ernie_chan_qt/results")
STOCK_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

MIN_ROWS = 1500   # ~6 years minimum

# ── Known pairs: hard skip ────────────────────────────────────────────────────
KNOWN = {
    frozenset({"TCS",        "INFY"}),           # portfolio
    frozenset({"NTPC",       "POWERGRID"}),       # portfolio
    frozenset({"BAJAJFINSV", "BAJFINANCE"}),      # portfolio
    frozenset({"ASIANPAINT", "BERGEPAINT"}),      # watchlist
    frozenset({"HINDPETRO",  "BPCL"}),            # rejected
    frozenset({"HINDPETRO",  "IOC"}),             # rejected
    frozenset({"GAIL",       "PETRONET"}),        # rejected
    frozenset({"IGL",        "MGL"}),             # rejected
    frozenset({"ACC",        "AMBUJACEMENT"}),    # rejected
    frozenset({"NATIONALUM", "HINDALCO"}),        # rejected
    frozenset({"COALINDIA",  "NMDC"}),            # rejected
    frozenset({"MRF",        "APOLLOTYRE"}),      # rejected
    frozenset({"SUNPHARMA",  "CIPLA"}),           # rejected
    frozenset({"DIVISLAB",   "AUROPHARMA"}),      # rejected
    frozenset({"SBIN",       "BANKBARODA"}),      # rejected
    frozenset({"FEDERALBNK", "INDUSINDBK"}),      # rejected
    frozenset({"HCLTECH",    "WIPRO"}),           # rejected
    frozenset({"HAVELLS",    "POLYCAB"}),         # rejected
}

# ── Sectors: (name, [(sym, yf_ticker, lot_size), ...]) ───────────────────────
# Priority A → B → C → D
SECTORS = [
    # ── PRIORITY A ──────────────────────────────────────────────────────────
    ("PSU Finance", [
        ("PFC",        "PFC.NS",        2700),
        ("RECLTD",     "RECLTD.NS",     2700),
        ("IRFC",       "IRFC.NS",       9000),
        ("HUDCO",      "HUDCO.NS",      7200),
        ("CANFINHOME", "CANFINHOME.NS", 1200),
        ("PNBHOUSING", "PNBHOUSING.NS", 1400),
    ]),
    ("Power — PSU", [
        ("NTPC",      "NTPC.NS",      3250),
        ("POWERGRID", "POWERGRID.NS", 4200),
        ("NHPC",      "NHPC.NS",      30000),
        ("SJVN",      "SJVN.NS",      6000),
    ]),
    ("Defence PSU", [
        ("BEL",       "BEL.NS",        3750),
        ("HAL",       "HAL.NS",         300),
        ("COCHINSHIP","COCHINSHIP.NS",   500),
        ("MAZAGON",   "MAZDOCK.NS",      250),
    ]),
    ("Auto — 2W", [
        ("HEROMOTOCO","HEROMOTOCO.NS",  300),
        ("BAJAJ-AUTO","BAJAJAUTO.NS",    75),
        ("EICHERMOT", "EICHERMOT.NS",   200),
        ("TVSMOTORS", "TVSMOTOR.NS",    350),
    ]),
    ("Steel", [
        ("TATASTEEL", "TATASTEEL.NS",  5500),
        ("JSWSTEEL",  "JSWSTEEL.NS",   1350),
        ("SAIL",      "SAIL.NS",       10500),
        ("JSPL",      "JINDALSTEL.NS", 1250),
    ]),
    ("Insurance", [
        ("HDFCLIFE",  "HDFCLIFE.NS",   1100),
        ("SBILIFE",   "SBILIFE.NS",     750),
        ("ICICIPRULI","ICICIPRULI.NS", 1500),
        # LICI (2022), STARHEALTH (2021) — short history, excluded
    ]),

    # ── PRIORITY B ──────────────────────────────────────────────────────────
    ("IT Services", [
        ("TCS",        "TCS.NS",        150),
        ("INFY",       "INFY.NS",       600),
        ("WIPRO",      "WIPRO.NS",     3000),
        ("HCLTECH",    "HCLTECH.NS",    700),
        ("TECHM",      "TECHM.NS",      600),
        ("MPHASIS",    "MPHASIS.NS",    300),
        ("LTIMINDTREE","LTIM.NS",       150),
        ("PERSISTENT", "PERSISTENT.NS", 150),
        ("COFORGE",    "COFORGE.NS",    200),
        ("OFSS",       "OFSS.NS",       200),
        ("LTTS",       "LTTS.NS",       200),
    ]),
    ("NBFC", [
        ("BAJFINANCE", "BAJFINANCE.NS",  125),
        ("BAJAJFINSV", "BAJAJFINSV.NS",  500),
        ("CHOLAFIN",   "CHOLAFIN.NS",    500),
        ("MUTHOOTFIN", "MUTHOOTFIN.NS",  750),
        ("MANAPPURAM", "MANAPPURAM.NS", 5000),
        ("SHRIRAMFIN", "SHRIRAMFIN.NS",  300),
        ("M&MFIN",     "M&MFIN.NS",    3000),
        ("LICHSGFIN",  "LICHSGFIN.NS", 1000),
    ]),
    ("Capital Goods", [
        ("LT",         "LT.NS",         450),
        ("SIEMENS",    "SIEMENS.NS",    275),
        ("ABB",        "ABB.NS",        500),
        ("BHEL",       "BHEL.NS",      7000),
        ("TIINDIA",    "TIINDIA.NS",    350),
        ("CUMMINSIND", "CUMMINSIND.NS", 600),
        ("GRINDWELL",  "GRINDWELL.NS",  900),
    ]),
    ("Consumer Durables", [
        ("TITAN",    "TITAN.NS",    375),
        ("HAVELLS",  "HAVELLS.NS",  300),
        ("POLYCAB",  "POLYCAB.NS",  150),
        ("CROMPTON", "CROMPTON.NS",2000),
        ("VOLTAS",   "VOLTAS.NS",   750),
        ("BLUESTAR", "BLUESTAR.NS", 500),
        ("KEI",      "KEI.NS",      500),
    ]),
    ("Cement", [
        ("ULTRACEMCO",  "ULTRACEMCO.NS",  250),
        ("AMBUJACEMENT","AMBUJACEM.NS",   600),
        ("ACC",         "ACC.NS",         475),
        ("SHREECEM",    "SHREECEM.NS",     25),
        ("JKCEMENT",    "JKCEMENT.NS",    200),
        ("DALBHARAT",   "DALBHARAT.NS",   175),
        ("RAMCOCEM",    "RAMCOCEM.NS",    750),
    ]),
    ("Auto — 4W / Commercial", [
        ("MARUTI",    "MARUTI.NS",     100),
        ("M&M",       "M&M.NS",        700),
        ("TATAMOTORS","TATAMOTORS.NS",1400),
        ("ASHOKLEY",  "ASHOKLEY.NS",  5500),
    ]),
    ("Auto Components", [
        ("APOLLOTYRE","APOLLOTYRE.NS",5500),
        ("MRF",       "MRF.NS",         24),
        ("CEATLTD",   "CEATLTD.NS",    400),
        ("BALKRISIND","BALKRISIND.NS",  400),
        ("BHARATFORG","BHARATFORG.NS",1000),
        ("MOTHERSON", "MOTHERSON.NS", 4500),
    ]),

    # ── PRIORITY C ──────────────────────────────────────────────────────────
    ("Private Banks", [
        ("HDFCBANK",   "HDFCBANK.NS",    550),
        ("ICICIBANK",  "ICICIBANK.NS",   700),
        ("KOTAKBANK",  "KOTAKBANK.NS",   400),
        ("AXISBANK",   "AXISBANK.NS",   1200),
        ("INDUSINDBK", "INDUSINDBK.NS",  700),
        ("FEDERALBNK", "FEDERALBNK.NS",10000),
        ("BANDHANBNK", "BANDHANBNK.NS", 5000),
        ("AUBANK",     "AUBANK.NS",     1000),
        ("IDFCFIRSTB", "IDFCFIRSTB.NS", 9000),
        ("RBLBANK",    "RBLBANK.NS",    4000),
    ]),
    ("PSU Banks", [
        ("SBIN",       "SBIN.NS",       1500),
        ("BANKBARODA", "BANKBARODA.NS", 3500),
        ("PNB",        "PNB.NS",        8000),
        ("CANARABANK", "CANARABANK.NS", 5000),
        ("UNIONBANK",  "UNIONBANK.NS",  8000),
        ("INDIANB",    "INDIANB.NS",     700),
    ]),
    ("Pharma", [
        ("SUNPHARMA",  "SUNPHARMA.NS",   700),
        ("CIPLA",      "CIPLA.NS",       650),
        ("DRREDDY",    "DRREDDY.NS",     125),
        ("LUPIN",      "LUPIN.NS",      1000),
        ("DIVISLAB",   "DIVISLAB.NS",    200),
        ("AUROPHARMA", "AUROPHARMA.NS", 3500),
        ("BIOCON",     "BIOCON.NS",     2700),
        ("ALKEM",      "ALKEM.NS",       300),
        ("TORNTPHARM", "TORNTPHARM.NS",  500),
        ("IPCALAB",    "IPCALAB.NS",     700),
        ("GLENMARK",   "GLENMARK.NS",   1800),
    ]),
    ("FMCG", [
        ("HINDUNILVR", "HINDUNILVR.NS",  300),
        ("ITC",        "ITC.NS",        3200),
        ("BRITANNIA",  "BRITANNIA.NS",   200),
        ("NESTLEIND",  "NESTLEIND.NS",   100),
        ("DABUR",      "DABUR.NS",      1250),
        ("MARICO",     "MARICO.NS",     1200),
        ("COLPAL",     "COLPAL.NS",      700),
        ("GODREJCP",   "GODREJCP.NS",    500),
        ("TATACONSUM", "TATACONSUM.NS",  750),
        ("EMAMILTD",   "EMAMILTD.NS",    600),
    ]),

    # ── PRIORITY D ──────────────────────────────────────────────────────────
    ("Chemicals", [
        ("PIDILITIND", "PIDILITIND.NS",  375),
        ("SRF",        "SRF.NS",         250),
        ("DEEPAKNTR",  "DEEPAKNTR.NS",   350),
        ("NAVINFLUOR", "NAVINFLUOR.NS",  100),
        ("AARTIIND",   "AARTIIND.NS",   1700),
        ("ATUL",       "ATUL.NS",        300),
    ]),
    ("Real Estate", [
        ("DLF",        "DLF.NS",        3300),
        ("GODREJPROP", "GODREJPROP.NS",  425),
        ("OBEROIRLTY", "OBEROIRLTY.NS",  800),
        ("PRESTIGE",   "PRESTIGE.NS",    750),
        ("BRIGADE",    "BRIGADE.NS",    1000),
        ("PHOENIXLTD", "PHOENIXLTD.NS",  500),
    ]),
    ("Power — Private", [
        ("TATAPOWER",  "TATAPOWER.NS",  4950),
        ("TORNTPOWER", "TORNTPOWER.NS",  500),
        ("CESC",       "CESC.NS",       1050),
        ("JSWENERGY",  "JSWENERGY.NS",  2000),
    ]),
    ("Metals — Non-ferrous", [
        ("HINDALCO",   "HINDALCO.NS",   1075),
        ("NATIONALUM", "NATIONALUM.NS", 7100),
        ("VEDL",       "VEDL.NS",       2750),
    ]),
    ("Mining", [
        ("COALINDIA",  "COALINDIA.NS",  4200),
        ("NMDC",       "NMDC.NS",      18000),
        ("MOIL",       "MOIL.NS",       3000),
    ]),
    ("OMC", [
        ("BPCL",      "BPCL.NS",      3600),
        ("IOC",       "IOC.NS",       2500),
        ("HINDPETRO", "HINDPETRO.NS", 2100),
    ]),
    ("Oil & Gas", [
        ("ONGC",    "ONGC.NS",    9750),
        ("OIL",     "OIL.NS",     2200),
        ("GAIL",    "GAIL.NS",    6700),
        ("PETRONET","PETRONET.NS",3000),
    ]),
    ("City Gas", [
        ("IGL",      "IGL.NS",       1375),
        ("MGL",      "MGL.NS",        400),
        ("GUJGASLTD","GUJGASLTD.NS", 1000),
    ]),
    ("Paints", [
        ("ASIANPAINT","ASIANPAINT.NS", 200),
        ("BERGEPAINT","BERGEPAINT.NS",1100),
        ("KANSAINER", "KANSAINER.NS",  300),
    ]),
    ("Retail", [
        ("TRENT",   "TRENT.NS",    350),
        ("DMART",   "DMART.NS",    150),
        ("JUBLFOOD","JUBLFOOD.NS",1250),
    ]),
    ("Telecom", [
        ("BHARTIARTL","BHARTIARTL.NS", 950),
        ("IDEA",      "IDEA.NS",     140000),
    ]),
    ("Hotels", [
        ("INDHOTEL","INDHOTEL.NS",2100),
        ("EIHOTEL", "EIHOTEL.NS", 2400),
    ]),
]


# ── Stock loader ──────────────────────────────────────────────────────────────
def load_stock(sym, yf_ticker):
    cache = STOCK_DIR / f"{sym}_yf.parquet"
    if cache.exists():
        s = pd.read_parquet(cache)["close"]
        print(f"    {sym:<14} cached  {len(s)} rows")
        return s
    try:
        raw = yf.download(yf_ticker, start="2015-01-01", end="2024-05-28",
                          auto_adjust=True, progress=False)
        if raw.empty:
            print(f"    {sym:<14} EMPTY download"); return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.index = pd.to_datetime(raw.index).normalize()
        close = raw["Close"].dropna()
        if len(close) < MIN_ROWS:
            print(f"    {sym:<14} only {len(close)} rows (short history)"); return None
        close.to_frame("close").to_parquet(cache)
        print(f"    {sym:<14} {len(close)} rows  {close.index[0].date()} → {close.index[-1].date()}")
        return close
    except Exception as e:
        print(f"    {sym:<14} ERROR: {e}"); return None


# ── Pair screener ─────────────────────────────────────────────────────────────
def screen_pair(sa, va, la, sb, vb, lb):
    df = pd.DataFrame({sa: va, sb: vb}).dropna()
    if len(df) < MIN_ROWS:
        return None
    pa, pb = df[sa].values, df[sb].values

    res   = OLS(pa, add_constant(pb)).fit()
    _, beta = res.params
    spread  = pa - beta * pb

    adf_r = adfuller(spread, autolag="AIC")
    stat, pval, crit = adf_r[0], adf_r[1], adf_r[4]
    level = ("1%"  if stat < crit["1%"]  else
             "5%"  if stat < crit["5%"]  else
             "10%" if stat < crit["10%"] else "FAIL")

    phi = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
    hl  = -np.log(2) / np.log(1 + phi) if phi < 0 else 999

    shares_b = abs(beta) * la
    best_lb  = max(1, round(shares_b / lb))
    actual_b = best_lb * lb
    imb      = abs(actual_b - shares_b) / shares_b * 100 if shares_b > 0 else 99
    rev      = False

    if imb > 40 and beta != 0:
        res_r = OLS(pb, add_constant(pa)).fit()
        _, beta_r = res_r.params
        shares_ar = abs(beta_r) * lb
        best_lar  = max(1, round(shares_ar / la))
        actual_ar = best_lar * la
        imb_r = abs(actual_ar - shares_ar) / shares_ar * 100 if shares_ar > 0 else 99
        if imb_r < imb:
            sa, sb, la, lb = sb, sa, lb, la
            beta, imb, best_lb = beta_r, imb_r, best_lar
            rev = True

    avg_a  = pa[-252:].mean()
    avg_b  = pb[-252:].mean()
    margin = int((avg_a * la + avg_b * best_lb * lb) * 0.15)
    viable = (stat < crit["10%"]) and imb <= 40 and hl < 300 and hl > 0

    return dict(
        A=sa, B=sb, lotA=la, lotB_lots=best_lb, lotB_unit=lb,
        beta=round(beta, 4), r2=round(res.rsquared, 3),
        stat=round(stat, 3), pval=round(pval, 4),
        level=level, hl=round(hl, 1), imb=round(imb, 1),
        margin=margin, viable=viable, rev=rev, rows=len(df),
    )


# ── Main loop ─────────────────────────────────────────────────────────────────
all_results = []
SEP  = "=" * 76
SEP2 = "─" * 76

for sector_name, stocks in SECTORS:
    n_stocks = len(stocks)
    n_pairs  = n_stocks * (n_stocks - 1) // 2
    print(f"\n{SEP}")
    print(f"  {sector_name}  ({n_stocks} stocks → {n_pairs} pairs)")
    print(SEP)

    stock_data = {}
    for sym, yf_t, lot in stocks:
        s = load_stock(sym, yf_t)
        if s is not None:
            stock_data[sym] = (s, lot)

    if len(stock_data) < 2:
        print("  Not enough stocks loaded, skip sector"); continue

    print(f"\n  {'Pair':<30} {'ADF':>7} {'p':>6} {'Lvl':>5} "
          f"{'HL':>5} {'imb':>5}   {'Margin':>10}   R²")
    print(f"  {SEP2}")

    viable_sector = []
    for (sa, (va, la)), (sb, (vb, lb)) in combinations(stock_data.items(), 2):
        if frozenset({sa, sb}) in KNOWN:
            print(f"  ---  {sa}/{sb}  [known — skip]")
            continue
        r = screen_pair(sa, va, la, sb, vb, lb)
        if r is None:
            continue
        r["sector"] = sector_name
        all_results.append(r)
        if r["viable"]:
            viable_sector.append(r)

        flag = ("✓✓" if r["level"] == "1%"  else
                "✓ " if r["level"] == "5%"  else
                "~  " if r["level"] == "10%" else "✗  ")
        pair_str = f"{r['A']}/{r['B']}" + ("[r]" if r["rev"] else "")
        print(f"  {flag}  {pair_str:<28} {r['stat']:>7.3f} {r['pval']:>6.4f} "
              f"{r['level']:>5} {r['hl']:>5.0f} {r['imb']:>4.0f}%"
              f"   Rs{r['margin']:>8,}   {r['r2']:.3f}")

    print(f"\n  Sector viable: {len(viable_sector)}")

# ── Save results ──────────────────────────────────────────────────────────────
df_out = pd.DataFrame(all_results)
if not df_out.empty:
    out_path = RESULTS_DIR / "sector_screen_results.csv"
    df_out.to_csv(out_path, index=False)
    print(f"\n\nResults saved → {out_path}")

# ── Final summary ─────────────────────────────────────────────────────────────
print(f"\n\n{SEP}")
print(f"  VIABLE PAIRS  (ADF ≤ 10%, imb ≤ 40%, 0 < HL < 300d)")
print(SEP)
print(f"  {'Pair':<30} {'Sector':<22} {'ADF':>5} {'HL':>5} {'imb':>5}   Margin")
print(f"  {SEP2}")

viable = sorted([r for r in all_results if r["viable"]], key=lambda x: x["stat"])
for r in viable:
    mark = "✓✓" if r["level"] == "1%" else "✓ " if r["level"] == "5%" else "~ "
    pair_str = f"{r['A']}/{r['B']}" + ("[r]" if r["rev"] else "")
    print(f"  {mark}  {pair_str:<30} {r['sector']:<22} "
          f"{r['level']:>5} {r['hl']:>5.0f} {r['imb']:>4.0f}%   Rs{r['margin']:,}")

print(f"\n  Tested: {len(all_results)}   Viable: {len(viable)}")
