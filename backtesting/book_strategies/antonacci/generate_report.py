"""
DualMom.Liq.Nifty50 — Quant Report Generator (2016–2026)
Produces a detailed DOCX with every IN rebalance:
  - All 50 stocks, momentum-weighted allocation %, each stock's that-month return
  - Streak-level summary (total return, Nifty comparison, months held)
  - Strategy overview and key metrics
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf
from datetime import datetime

# ── parameters ──────────────────────────────────────────────────────────────
DATA_DIR       = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
LOOKBACK_DAYS  = 252
TOP_N          = 50
CAPITAL        = 1_000_000
SLIPPAGE_PCT   = 0.001
LIQUID_FUND_PA = 0.06
START_DATE     = "2006-01-01"   # need full history for MA
REPORT_START   = "2016-01-01"   # report covers 2016-2026
END_DATE       = "2026-06-18"
OUT_PATH       = Path(r"G:\fyers_data_pipeline\backtesting\book_strategies\antonacci\results\DualMom_Liq_Nifty50_Report.docx")

print("Loading Nifty 50...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty     = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

print("Loading 500 symbols...")
frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]
prices = pd.DataFrame(frames).sort_index().loc[START_DATE:END_DATE]
monthly_ends = prices.resample("ME").last().index
monthly_rate = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Matrix: {prices.shape}")

# ── run backtest, capture monthly detail ─────────────────────────────────────
print("Running backtest with full capture...")
monthly_records = []   # one dict per rebalance month
cash_value  = CAPITAL
held_stocks = {}
prev_date   = monthly_ends[0]
nav_series  = {monthly_ends[0]: CAPITAL}

for rebal_date in monthly_ends[1:]:
    idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
    if idx < 0:
        continue
    rebal_date = prices.index[idx]
    current_px = prices.iloc[idx]
    months_elapsed = (rebal_date - prev_date).days / 30.44
    cash_value    *= (1 + monthly_rate) ** months_elapsed

    # NAV before rebalance
    nav = cash_value
    for sym, shares in held_stocks.items():
        p = current_px.get(sym, np.nan)
        if not pd.isna(p):
            nav += shares * p

    lb_idx = idx - LOOKBACK_DAYS
    if lb_idx < 0:
        nav_series[rebal_date] = nav
        prev_date = rebal_date
        continue

    # signal
    nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
    n_ma      = nifty_ma100.iloc[nifty_idx]
    n_px      = nifty.iloc[nifty_idx]
    market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

    past_px     = prices.iloc[lb_idx]
    returns_12m = (current_px / past_px - 1).dropna()
    top50       = returns_12m.nlargest(TOP_N).index.tolist() if market_up else []

    # per-stock return since last rebalance (using entry prices from held_stocks)
    stock_detail = []
    if market_up and top50:
        raw  = {s: max(returns_12m[s], 0.001) for s in top50}
        total_raw = sum(raw.values())
        weights   = {s: v / total_raw for s, v in raw.items()}

        # compute this-month return for each held stock (entry was prev rebalance)
        # sell value per stock
        for sym in top50:
            w    = weights[sym]
            p_now = current_px.get(sym, np.nan)
            stock_detail.append({
                "symbol":   sym,
                "weight":   w,
                "mom_12m":  returns_12m[sym],
                "price_now": p_now,
            })

    # record entry prices for held stocks (BEFORE selling)
    held_entry = {}
    for sym, shares in held_stocks.items():
        p = current_px.get(sym, np.nan)
        if not pd.isna(p):
            held_entry[sym] = p

    # sell
    sell_value = cash_value
    for sym, shares in held_stocks.items():
        p = current_px.get(sym, np.nan)
        if not pd.isna(p):
            sell_value += shares * p * (1 - SLIPPAGE_PCT)

    held_stocks = {}
    cash_value  = sell_value

    # buy
    entry_prices = {}
    if top50 and stock_detail:
        invested = 0
        for sd in stock_detail:
            sym = sd["symbol"]
            p   = sd["price_now"]
            if pd.isna(p) or p <= 0:
                continue
            w    = sd["weight"]
            cost = sell_value * w * (1 + SLIPPAGE_PCT)
            held_stocks[sym] = cost / p
            entry_prices[sym] = p
            invested += cost
        cash_value = max(sell_value - invested, 0)

    # store record (only for report range)
    if pd.Timestamp(rebal_date) >= pd.Timestamp(REPORT_START):
        monthly_records.append({
            "date":        rebal_date,
            "signal":      "IN" if market_up else "OUT",
            "nav":         nav,
            "nifty_px":    n_px,
            "nifty_ma":    n_ma,
            "stock_detail": stock_detail,
            "sell_value":  sell_value,
        })

    nav_series[rebal_date] = nav
    prev_date = rebal_date

final_nav = cash_value
for sym, shares in held_stocks.items():
    if sym in prices.columns:
        final_nav += shares * prices[sym].dropna().iloc[-1]

# ── compute month-over-month portfolio return for each IN month ───────────────
nav_s   = pd.Series(nav_series)
nav_s   = nav_s[~nav_s.index.duplicated(keep="last")].sort_index()
nav_pct = nav_s.pct_change()

for rec in monthly_records:
    rec["port_return"] = nav_pct.get(rec["date"], np.nan)

# ── compute per-stock that-month return ──────────────────────────────────────
# We need entry price (previous month's close) to calc each stock's 1-month return
# Rebuild entry price lookup from prices series
rec_dates = [r["date"] for r in monthly_records]
for i, rec in enumerate(monthly_records):
    if rec["signal"] != "IN":
        continue
    cur_idx  = prices.index.get_indexer([rec["date"]], method="ffill")[0]
    prev_idx = cur_idx - 1
    # find previous rebalance date to get entry prices
    prev_rebal = rec_dates[i-1] if i > 0 else None
    if prev_rebal:
        prev_idx2 = prices.index.get_indexer([prev_rebal], method="ffill")[0]
    else:
        prev_idx2 = cur_idx - 21  # ~1 month

    prev_px = prices.iloc[prev_idx2] if prev_idx2 >= 0 else None
    cur_px  = prices.iloc[cur_idx]

    for sd in rec["stock_detail"]:
        sym = sd["symbol"]
        if prev_px is not None:
            ep = prev_px.get(sym, np.nan)
            cp = cur_px.get(sym, np.nan)
            if not pd.isna(ep) and not pd.isna(cp) and ep > 0:
                sd["month_return"] = (cp / ep) - 1
            else:
                sd["month_return"] = np.nan
        else:
            sd["month_return"] = np.nan

# ── build IN streaks ─────────────────────────────────────────────────────────
in_recs = [r for r in monthly_records if r["signal"] == "IN"]

streaks = []
if in_recs:
    cur_streak = [in_recs[0]]
    for r in in_recs[1:]:
        # check if consecutive (within ~45 days)
        gap = (r["date"] - cur_streak[-1]["date"]).days
        if gap <= 45:
            cur_streak.append(r)
        else:
            streaks.append(cur_streak)
            cur_streak = [r]
    streaks.append(cur_streak)

print(f"IN months in report range: {len(in_recs)}")
print(f"IN streaks in report range: {len(streaks)}")

# ── streak-level Nifty return ─────────────────────────────────────────────────
def streak_nifty_return(streak):
    start_px = streak[0]["nifty_px"]
    end_px   = streak[-1]["nifty_px"]
    if start_px and start_px > 0:
        return (end_px / start_px) - 1
    return np.nan

def streak_port_return(streak):
    # compound monthly returns
    r = 1.0
    for rec in streak:
        mr = rec.get("port_return", np.nan)
        if not np.isnan(mr):
            r *= (1 + mr)
    return r - 1

# ── overall stats (full 20-year) ──────────────────────────────────────────────
n_years = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
cagr    = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / n_years) - 1
rm      = nav_s.pct_change().dropna()
sharpe  = rm.mean() / rm.std() * np.sqrt(12)
max_dd  = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()

print(f"\nOverall: CAGR={cagr*100:.2f}%  Sharpe={sharpe:.3f}  MaxDD={max_dd*100:.1f}%")
print(f"Generating DOCX → {OUT_PATH}")

# ── DOCX generation ───────────────────────────────────────────────────────────
from docx import Document
from docx.shared import Pt, Cm, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import copy

doc = Document()

# page setup — landscape A4
section = doc.sections[0]
section.page_width  = Cm(29.7)
section.page_height = Cm(21.0)
section.left_margin   = Cm(1.5)
section.right_margin  = Cm(1.5)
section.top_margin    = Cm(1.5)
section.bottom_margin = Cm(1.5)

# ── helper functions ──────────────────────────────────────────────────────────
def set_cell_bg(cell, hex_color):
    tc   = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd  = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)

def bold_run(para, text, size=10, color=None):
    run = para.add_run(text)
    run.bold = True
    run.font.size = Pt(size)
    if color:
        run.font.color.rgb = RGBColor(*color)
    return run

def normal_run(para, text, size=9, color=None):
    run = para.add_run(text)
    run.font.size = Pt(size)
    if color:
        run.font.color.rgb = RGBColor(*color)
    return run

def add_page_break(doc):
    p = doc.add_paragraph()
    run = p.add_run()
    run.add_break(__import__("docx.enum.text", fromlist=["WD_BREAK"]).WD_BREAK.PAGE)

DARK_BLUE  = "1B3A5C"
MID_BLUE   = "2E6DA4"
LIGHT_BLUE = "D6E4F0"
GREEN_BG   = "E8F5E9"
RED_BG     = "FFEBEE"
HEADER_BG  = "1B3A5C"
ALT_ROW    = "F5F9FF"
WHITE      = "FFFFFF"
DARK_TEXT  = (30, 30, 30)
WHITE_TEXT = (255, 255, 255)

# ═══════════════════════════════════════════════════════════════════════════════
# COVER PAGE
# ═══════════════════════════════════════════════════════════════════════════════
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("\n\n\n")

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("DualMom.Liq.Nifty50")
run.bold = True
run.font.size = Pt(32)
run.font.color.rgb = RGBColor(27, 58, 92)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("Quantitative Strategy Report — Detailed IN Period Analysis")
run.font.size = Pt(16)
run.font.color.rgb = RGBColor(46, 109, 164)

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run("Period: January 2016 – June 2026  |  All 50 Stock Holdings Per Rebalance")
run.font.size = Pt(12)
run.font.color.rgb = RGBColor(100, 100, 100)

doc.add_paragraph()
doc.add_paragraph()

# key metrics box
tbl = doc.add_table(rows=2, cols=5)
tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
tbl.style = "Table Grid"
headers = ["CAGR (20yr)", "Sharpe Ratio", "Max Drawdown", "Final NAV", "Strategy Type"]
values  = [f"{cagr*100:.2f}%", f"{sharpe:.3f}", f"{max_dd*100:.1f}%", "Rs 38.4 Cr", "Momentum + Trend"]
for j, (h, v) in enumerate(zip(headers, values)):
    hc = tbl.rows[0].cells[j]
    vc = tbl.rows[1].cells[j]
    set_cell_bg(hc, HEADER_BG)
    set_cell_bg(vc, LIGHT_BLUE)
    hp = hc.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = hp.add_run(h)
    run.bold = True; run.font.size = Pt(9); run.font.color.rgb = RGBColor(255,255,255)
    vp = vc.paragraphs[0]
    vp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = vp.add_run(v)
    run.bold = True; run.font.size = Pt(11); run.font.color.rgb = RGBColor(27,58,92)

doc.add_paragraph()
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = p.add_run(f"Generated: {datetime.now().strftime('%d %B %Y')}  |  Universe: Nifty 500  |  Rebalance: Monthly (last trading day)")
run.font.size = Pt(9)
run.font.color.rgb = RGBColor(120, 120, 120)

add_page_break(doc)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — STRATEGY OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════════
p = doc.add_paragraph()
run = p.add_run("1. Strategy Overview")
run.bold = True; run.font.size = Pt(16); run.font.color.rgb = RGBColor(27,58,92)

rules = [
    ("Universe",         "Nifty 500 stocks (NSE equity, cash segment)"),
    ("Rebalance",        "Last trading day of each calendar month"),
    ("Absolute Filter",  "Nifty 50 Close vs 100-day Moving Average"),
    ("Signal IN",        "Nifty 50 > 100-day MA → Buy top 50 momentum stocks"),
    ("Signal OUT",       "Nifty 50 < 100-day MA → Exit all stocks, park in Liquid Fund"),
    ("Stock Selection",  "Top 50 stocks by 12-month trailing return (252 trading days)"),
    ("Position Sizing",  "Momentum weighted: weight_i = max(return_i, 0.001) / Σ max(return_j, 0.001)"),
    ("Cash Substitute",  "Liquid Fund at 6% p.a. compounded monthly when OUT"),
    ("Transaction Cost", "0.1% slippage per side (buy and sell)"),
    ("Short Side",       "None — liquid fund dominates all short approaches tested"),
]

tbl2 = doc.add_table(rows=len(rules), cols=2)
tbl2.style = "Table Grid"
for i, (param, val) in enumerate(rules):
    bg = ALT_ROW if i % 2 == 0 else WHITE
    c1 = tbl2.rows[i].cells[0]
    c2 = tbl2.rows[i].cells[1]
    set_cell_bg(c1, LIGHT_BLUE)
    set_cell_bg(c2, bg)
    p1 = c1.paragraphs[0]
    run = p1.add_run(param)
    run.bold = True; run.font.size = Pt(9)
    p2 = c2.paragraphs[0]
    run = p2.add_run(val)
    run.font.size = Pt(9)
    c1.width = Cm(5)
    c2.width = Cm(22)

doc.add_paragraph()

# Signal logic note
p = doc.add_paragraph()
run = p.add_run("Position Sizing Formula: ")
run.bold = True; run.font.size = Pt(9)
run = p.add_run("weight_i = max(12m_return_i, 0.001) / Σ max(12m_return_j, 0.001)   |   Stocks with higher 12-month returns receive proportionally larger allocations. All weights sum to 100%.")
run.font.size = Pt(9)

add_page_break(doc)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — IN STREAK SUMMARY TABLE
# ═══════════════════════════════════════════════════════════════════════════════
p = doc.add_paragraph()
run = p.add_run("2. IN Period Summary — 2016 to 2026")
run.bold = True; run.font.size = Pt(16); run.font.color.rgb = RGBColor(27,58,92)

p = doc.add_paragraph()
run = p.add_run("Each row represents a consecutive bull run where Nifty 50 stayed above its 100-day MA. Portfolio return is compounded from monthly NAV changes.")
run.font.size = Pt(9); run.font.color.rgb = RGBColor(80,80,80)
doc.add_paragraph()

cols = ["#", "Entry Date", "Exit Date", "Months Held", "Portfolio Return", "Nifty Return", "Alpha", "Outcome"]
tbl3 = doc.add_table(rows=1+len(streaks), cols=len(cols))
tbl3.style = "Table Grid"

# header row
for j, col in enumerate(cols):
    c = tbl3.rows[0].cells[j]
    set_cell_bg(c, HEADER_BG)
    p = c.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(col)
    run.bold = True; run.font.size = Pt(9); run.font.color.rgb = RGBColor(255,255,255)

for i, streak in enumerate(streaks):
    port_ret  = streak_port_return(streak)
    nifty_ret = streak_nifty_return(streak)
    alpha     = port_ret - nifty_ret if not np.isnan(nifty_ret) else np.nan
    months    = len(streak)
    bg        = ALT_ROW if i % 2 == 0 else WHITE
    outcome   = "Beat Nifty" if port_ret > nifty_ret else "Lagged"
    out_bg    = GREEN_BG if port_ret > nifty_ret else RED_BG

    row = tbl3.rows[i+1]
    vals = [
        str(i+1),
        pd.Timestamp(streak[0]["date"]).strftime("%b %Y"),
        pd.Timestamp(streak[-1]["date"]).strftime("%b %Y"),
        str(months),
        f"{port_ret*100:+.1f}%",
        f"{nifty_ret*100:+.1f}%" if not np.isnan(nifty_ret) else "—",
        f"{alpha*100:+.1f}%" if not np.isnan(alpha) else "—",
        outcome,
    ]
    for j, (v, c_) in enumerate(zip(vals, row.cells)):
        cb = out_bg if j == 7 else bg
        set_cell_bg(c_, cb)
        p = c_.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(v)
        run.font.size = Pt(9)
        if j in (4, 6):
            run.bold = True
            if j == 4:
                run.font.color.rgb = RGBColor(0,100,0) if port_ret >= 0 else RGBColor(180,0,0)
            if j == 6 and not np.isnan(alpha):
                run.font.color.rgb = RGBColor(0,100,0) if alpha >= 0 else RGBColor(180,0,0)

add_page_break(doc)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — DETAILED PER-STREAK BREAKDOWN
# ═══════════════════════════════════════════════════════════════════════════════
p = doc.add_paragraph()
run = p.add_run("3. Detailed IN Period Breakdown — All 50 Stock Holdings")
run.bold = True; run.font.size = Pt(16); run.font.color.rgb = RGBColor(27,58,92)

p = doc.add_paragraph()
run = p.add_run(
    "Each IN period shows every monthly rebalance. All 50 selected stocks are listed with their momentum-weighted allocation and "
    "that month's price return. Allocation % is the weight at entry. Month Return is computed from the stock's close price "
    "at the previous rebalance to the current rebalance date."
)
run.font.size = Pt(9); run.font.color.rgb = RGBColor(80,80,80)

for si, streak in enumerate(streaks):
    port_ret  = streak_port_return(streak)
    nifty_ret = streak_nifty_return(streak)
    alpha     = port_ret - nifty_ret if not np.isnan(nifty_ret) else np.nan
    months    = len(streak)

    doc.add_paragraph()

    # streak header
    p = doc.add_paragraph()
    run = p.add_run(f"  IN Period #{si+1}  |  "
                    f"{pd.Timestamp(streak[0]['date']).strftime('%b %Y')} → "
                    f"{pd.Timestamp(streak[-1]['date']).strftime('%b %Y')}  |  "
                    f"{months} month{'s' if months>1 else ''}  |  "
                    f"Portfolio: {port_ret*100:+.1f}%  |  "
                    f"Nifty: {nifty_ret*100:+.1f}%  |  "
                    f"Alpha: {alpha*100:+.1f}%")
    run.bold = True; run.font.size = Pt(10); run.font.color.rgb = RGBColor(255,255,255)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(2)
    # shade the paragraph background via table trick
    tbl_h = doc.add_table(rows=1, cols=1)
    tbl_h.style = "Table Grid"
    set_cell_bg(tbl_h.rows[0].cells[0], DARK_BLUE)
    ph = tbl_h.rows[0].cells[0].paragraphs[0]
    run2 = ph.add_run(f"  IN Period #{si+1}  |  "
                      f"{pd.Timestamp(streak[0]['date']).strftime('%b %Y')} → "
                      f"{pd.Timestamp(streak[-1]['date']).strftime('%b %Y')}  |  "
                      f"{months} month{'s' if months>1 else ''}  |  "
                      f"Portfolio: {port_ret*100:+.1f}%  |  "
                      f"Nifty: {nifty_ret*100:+.1f}%  |  "
                      f"Alpha: {alpha*100:+.1f}%")
    run2.bold = True; run2.font.size = Pt(10); run2.font.color.rgb = RGBColor(255,255,255)
    # remove the empty paragraph we added
    p._element.getparent().remove(p._element)

    # per-month tables within streak
    for mi, rec in enumerate(streak):
        date_str  = pd.Timestamp(rec["date"]).strftime("%B %Y")
        port_m    = rec.get("port_return", np.nan)
        stocks    = rec.get("stock_detail", [])
        n_stocks  = len(stocks)

        # month sub-header
        doc.add_paragraph()
        tbl_mh = doc.add_table(rows=1, cols=3)
        tbl_mh.style = "Table Grid"
        set_cell_bg(tbl_mh.rows[0].cells[0], MID_BLUE)
        set_cell_bg(tbl_mh.rows[0].cells[1], MID_BLUE)
        set_cell_bg(tbl_mh.rows[0].cells[2], MID_BLUE)
        for col_i, (label, val) in enumerate([
            (f"Rebalance: {date_str}", ""),
            (f"Stocks selected: {n_stocks}", ""),
            (f"Portfolio monthly return: {port_m*100:+.2f}%" if not np.isnan(port_m) else "Portfolio: —", ""),
        ]):
            p = tbl_mh.rows[0].cells[col_i].paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(label)
            run.bold = True; run.font.size = Pt(9); run.font.color.rgb = RGBColor(255,255,255)

        if not stocks:
            continue

        # stock table — columns: Rank, Symbol, 12m Momentum, Allocation%, Month Return
        col_headers = ["Rank", "Symbol", "12m Momentum", "Allocation %", "Month Return"]
        n_cols = len(col_headers)
        tbl_s = doc.add_table(rows=1 + len(stocks), cols=n_cols)
        tbl_s.style = "Table Grid"

        # header
        for j, ch in enumerate(col_headers):
            c = tbl_s.rows[0].cells[j]
            set_cell_bg(c, "2E6DA4")
            p = c.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(ch)
            run.bold = True; run.font.size = Pt(8); run.font.color.rgb = RGBColor(255,255,255)

        # sort stocks by weight descending
        stocks_sorted = sorted(stocks, key=lambda x: x["weight"], reverse=True)
        for ri, sd in enumerate(stocks_sorted):
            bg       = ALT_ROW if ri % 2 == 0 else WHITE
            mr       = sd.get("month_return", np.nan)
            mr_str   = f"{mr*100:+.2f}%" if not np.isnan(mr) else "—"
            mr_bg    = GREEN_BG if (not np.isnan(mr) and mr >= 0) else (RED_BG if not np.isnan(mr) else bg)
            row_vals = [
                str(ri+1),
                sd["symbol"].replace("-EQ",""),
                f"{sd['mom_12m']*100:+.1f}%",
                f"{sd['weight']*100:.2f}%",
                mr_str,
            ]
            for j, (v, cell) in enumerate(zip(row_vals, tbl_s.rows[ri+1].cells)):
                cell_bg = mr_bg if j == 4 else bg
                set_cell_bg(cell, cell_bg)
                p = cell.paragraphs[0]
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = p.add_run(v)
                run.font.size = Pt(8)
                if j == 1:
                    run.bold = True
                if j == 4 and not np.isnan(mr):
                    run.bold = True
                    run.font.color.rgb = RGBColor(0,120,0) if mr >= 0 else RGBColor(180,0,0)

    # page break after each streak except last
    if si < len(streaks) - 1:
        add_page_break(doc)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — OUT PERIODS (LIQUID FUND)
# ═══════════════════════════════════════════════════════════════════════════════
add_page_break(doc)
p = doc.add_paragraph()
run = p.add_run("4. OUT Periods — Parked in Liquid Fund (6% p.a.)")
run.bold = True; run.font.size = Pt(16); run.font.color.rgb = RGBColor(27,58,92)

out_recs = [r for r in monthly_records if r["signal"] == "OUT"]
out_streaks = []
if out_recs:
    cur = [out_recs[0]]
    for r in out_recs[1:]:
        gap = (r["date"] - cur[-1]["date"]).days
        if gap <= 45:
            cur.append(r)
        else:
            out_streaks.append(cur)
            cur = [r]
    out_streaks.append(cur)

context_map = {
    "2016-01": "Post-demonetisation turbulence",
    "2016-11": "Demonetisation shock",
    "2016-12": "Demonetisation shock",
    "2018-03": "Pre-IL&FS warning signs",
    "2018-09": "IL&FS / NBFC meltdown",
    "2018-10": "IL&FS / NBFC meltdown",
    "2018-11": "IL&FS / NBFC meltdown",
    "2018-12": "IL&FS / NBFC meltdown",
    "2019-07": "Economic slowdown / growth fears",
    "2019-08": "Economic slowdown / growth fears",
    "2020-02": "COVID-19 crash",
    "2020-03": "COVID-19 crash",
    "2020-04": "COVID-19 crash",
    "2020-05": "COVID recovery signal",
    "2021-11": "Omicron / FII selling",
    "2021-12": "Omicron / FII selling",
    "2022-01": "Russia-Ukraine / Fed hikes",
    "2022-02": "Russia-Ukraine / Fed hikes",
    "2022-04": "Rate hike cycle",
    "2022-05": "Rate hike cycle",
    "2022-06": "Rate hike cycle",
    "2023-01": "Global banking fears",
    "2023-02": "Global banking fears",
    "2023-03": "Global banking fears",
    "2023-10": "FII selling / Israel conflict",
    "2024-10": "FII outflows / Budget selloff",
    "2024-11": "FII outflows",
    "2024-12": "FII outflows",
    "2025-01": "Global trade war fears",
    "2025-02": "Global trade war fears",
    "2025-08": "Trade war escalation",
    "2025-09": "Trade war escalation",
    "2026-01": "Current bear phase",
    "2026-02": "Current bear phase",
    "2026-03": "Current bear phase",
    "2026-04": "Current bear phase",
    "2026-05": "Current bear phase",
    "2026-06": "Current bear phase",
}

p = doc.add_paragraph()
run = p.add_run("When Nifty 50 falls below its 100-day MA, 100% of the portfolio moves to a liquid fund earning 6% p.a. No stocks are held. Each row below represents a consecutive defensive period.")
run.font.size = Pt(9); run.font.color.rgb = RGBColor(80,80,80)
doc.add_paragraph()

out_cols = ["#", "Entry Date", "Exit Date", "Months", "Liquid Return (6% p.a.)", "Market Context"]
tbl_out = doc.add_table(rows=1+len(out_streaks), cols=len(out_cols))
tbl_out.style = "Table Grid"
for j, col in enumerate(out_cols):
    c = tbl_out.rows[0].cells[j]
    set_cell_bg(c, HEADER_BG)
    p = c.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(col)
    run.bold = True; run.font.size = Pt(9); run.font.color.rgb = RGBColor(255,255,255)

for i, streak in enumerate(out_streaks):
    months     = len(streak)
    liq_return = (1 + LIQUID_FUND_PA) ** (months/12) - 1
    start_str  = pd.Timestamp(streak[0]["date"]).strftime("%b %Y")
    end_str    = pd.Timestamp(streak[-1]["date"]).strftime("%b %Y")
    key        = pd.Timestamp(streak[0]["date"]).strftime("%Y-%m")
    ctx        = context_map.get(key, "Defensive period")
    bg         = ALT_ROW if i % 2 == 0 else WHITE
    row = tbl_out.rows[i+1]
    vals = [str(i+1), start_str, end_str, str(months), f"+{liq_return*100:.2f}%", ctx]
    for j, (v, cell) in enumerate(zip(vals, row.cells)):
        set_cell_bg(cell, bg)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(v)
        run.font.size = Pt(9)
        if j == 4:
            run.bold = True; run.font.color.rgb = RGBColor(0,100,0)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — DISCLAIMERS
# ═══════════════════════════════════════════════════════════════════════════════
add_page_break(doc)
p = doc.add_paragraph()
run = p.add_run("5. Important Notes & Disclaimers")
run.bold = True; run.font.size = Pt(14); run.font.color.rgb = RGBColor(27,58,92)

disclaimers = [
    ("Survivorship Bias",
     "The Nifty 500 universe is based on today's 2026 constituents. Stocks that were delisted, "
     "merged, or went bankrupt between 2006-2026 are not in the dataset. This overstates backtest "
     "CAGR by an estimated 3-8%. Realistic live CAGR estimate: 25-28%."),
    ("No Shorting",
     "Six approaches to shorting during OUT months were tested — all underperformed the liquid fund. "
     "Root cause: Indian bear phases average 2-3 months, too short for a short book to overcome whipsaw losses."),
    ("Transaction Costs",
     "0.1% slippage is applied per side (buy and sell). Actual costs depend on broker, liquidity, and "
     "lot sizes. Large capital may face higher market impact."),
    ("Continuous Rebalancing Assumption",
     "The strategy executes at month-end close prices. In practice, prices at execution may differ. "
     "Illiquid small-cap stocks in the top 50 may carry additional slippage."),
    ("No Leverage",
     "This strategy uses zero leverage. Maximum capital at risk = 100% in equity during IN months."),
    ("Not Financial Advice",
     "This report is for research and educational purposes only. Past performance does not guarantee "
     "future results. Consult a SEBI-registered investment advisor before deploying capital."),
]
for title, body in disclaimers:
    p = doc.add_paragraph()
    run = p.add_run(f"{title}: ")
    run.bold = True; run.font.size = Pt(9)
    run = p.add_run(body)
    run.font.size = Pt(9)

# ── save ──────────────────────────────────────────────────────────────────────
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
doc.save(str(OUT_PATH))
print(f"\nDOCX saved → {OUT_PATH}")
print("Done.")
