"""
DualMom.Liq.Nifty50 — Bottom 10 vs Top 40 vs Nifty Annual Report (DOCX)
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
import yfinance as yf
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

DATA_DIR      = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
LOOKBACK_DAYS = 252
TOP_N         = 50
START_DATE    = "2006-01-01"
END_DATE      = "2026-06-18"
OUT_PATH      = Path(r"G:\fyers_data_pipeline\backtesting\book_strategies\antonacci\results\Bottom10_Top40_Report.docx")

# ── load data ─────────────────────────────────────────────────────────────────
print("Loading data...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty     = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]
prices = pd.DataFrame(frames).sort_index().loc[START_DATE:END_DATE]
monthly_ends = prices.resample("ME").last().index

# ── collect stocks held per year ──────────────────────────────────────────────
print("Extracting signals...")
yearly_held = {}
for rebal_date in monthly_ends[1:]:
    idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
    if idx < 0: continue
    rebal_date = prices.index[idx]
    lb_idx = idx - LOOKBACK_DAYS
    if lb_idx < 0: continue
    nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
    n_ma = nifty_ma100.iloc[nifty_idx]
    n_px = nifty.iloc[nifty_idx]
    if pd.isna(n_ma) or n_px <= n_ma: continue
    returns_12m = (prices.iloc[idx] / prices.iloc[lb_idx] - 1).dropna()
    top50 = returns_12m.nlargest(TOP_N).index.tolist()
    yr = pd.Timestamp(rebal_date).year
    if yr not in yearly_held:
        yearly_held[yr] = set()
    yearly_held[yr].update(top50)

# ── Nifty annual returns ──────────────────────────────────────────────────────
nifty_ye = nifty.resample("YE").last()
nifty_ys = nifty.resample("YS").first()
nifty_ann = {}
for yr_ts in nifty_ye.index:
    yr = yr_ts.year
    sp_vals = nifty_ys[nifty_ys.index.year == yr]
    ep_vals = nifty_ye[nifty_ye.index.year == yr]
    if len(sp_vals) and len(ep_vals) and sp_vals.iloc[0] > 0:
        nifty_ann[yr] = (ep_vals.iloc[0] / sp_vals.iloc[0] - 1) * 100

# ── compute annual stock returns and group top40 / bottom10 ───────────────────
print("Computing annual returns...")
yearly_data = {}
for yr in sorted(yearly_held.keys()):
    syms = list(yearly_held[yr])
    yr_prices = prices[prices.index.year == yr]
    if yr_prices.empty: continue
    start_px = yr_prices.iloc[0]
    end_px   = yr_prices.iloc[-1]
    stock_returns = {}
    for sym in syms:
        sp = start_px.get(sym, np.nan)
        ep = end_px.get(sym, np.nan)
        if not pd.isna(sp) and not pd.isna(ep) and sp > 0:
            stock_returns[sym] = (ep / sp - 1) * 100
    if len(stock_returns) < 10: continue

    sorted_stocks = sorted(stock_returns.items(), key=lambda x: x[1], reverse=True)
    top40   = sorted_stocks[:40]
    bottom10 = sorted_stocks[-10:]

    top40_avg    = np.mean([v for _, v in top40])
    bottom10_avg = np.mean([v for _, v in bottom10])
    nifty_ret    = nifty_ann.get(yr, np.nan)

    yearly_data[yr] = {
        "top40":         top40,
        "bottom10":      bottom10,
        "top40_avg":     top40_avg,
        "bottom10_avg":  bottom10_avg,
        "nifty_ret":     nifty_ret,
        "n_stocks":      len(stock_returns),
    }
    print(f"  {yr}: top40 avg={top40_avg:+.1f}%  bottom10 avg={bottom10_avg:+.1f}%  nifty={nifty_ret:+.1f}%")

# ── DOCX helpers ──────────────────────────────────────────────────────────────
def set_cell_bg(cell, hex_color):
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)

def add_run(para, text, bold=False, size=9, color=None, italic=False):
    run = para.add_run(text)
    run.bold = bold
    run.italic = italic
    run.font.size = Pt(size)
    if color:
        run.font.color.rgb = RGBColor(*color)
    return run

def add_page_break(doc):
    para = doc.add_paragraph()
    from docx.oxml.ns import qn as _qn
    from docx.oxml import OxmlElement as _OE
    run = para.add_run()
    br = _OE("w:br")
    br.set(_qn("w:type"), "page")
    run._r.append(br)

DARK_BLUE  = "1B3A5C"
MID_BLUE   = "2E6DA4"
LIGHT_BLUE = "D6E4F0"
GREEN_BG   = "C8E6C9"
RED_BG     = "FFCDD2"
AMBER_BG   = "FFF3E0"
ALT        = "F5F9FF"
WHITE      = "FFFFFF"

# ── build DOCX ────────────────────────────────────────────────────────────────
print("\nBuilding DOCX...")
doc = Document()
section = doc.sections[0]
section.page_width    = Cm(29.7)
section.page_height   = Cm(21.0)
section.left_margin   = Cm(1.5)
section.right_margin  = Cm(1.5)
section.top_margin    = Cm(1.5)
section.bottom_margin = Cm(1.5)

# ── COVER ─────────────────────────────────────────────────────────────────────
for _ in range(3): doc.add_paragraph()

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
add_run(p, "DualMom.Liq.Nifty50", bold=True, size=30, color=(27,58,92))

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
add_run(p, "Bottom 10 vs Top 40 vs Nifty — Annual Performance Report", size=15, color=(46,109,164))

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
add_run(p, f"Period: 2007 – 2025  |  Universe: Nifty 500  |  Generated: {datetime.now().strftime('%d %B %Y')}",
        size=10, color=(100,100,100))

doc.add_paragraph()
doc.add_paragraph()

# cover summary table
tbl = doc.add_table(rows=2, cols=3)
tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
tbl.style = "Table Grid"
hdrs = ["What is Top 40?", "What is Bottom 10?", "Benchmark"]
vals = ["Top 40 stocks by annual return\namong all stocks held in IN months",
        "Bottom 10 stocks by annual return\namong all stocks held in IN months",
        "Nifty 50 Index\ncalendar year return"]
for j in range(3):
    set_cell_bg(tbl.rows[0].cells[j], DARK_BLUE)
    set_cell_bg(tbl.rows[1].cells[j], LIGHT_BLUE)
    p = tbl.rows[0].cells[j].paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_run(p, hdrs[j], bold=True, size=9, color=(255,255,255))
    p = tbl.rows[1].cells[j].paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_run(p, vals[j], size=9, color=(27,58,92))

doc.add_paragraph()
p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
add_run(p, "Note: Returns are equal-weighted averages. Stocks are ranked by full calendar-year price return (Jan → Dec).",
        size=8, italic=True, color=(120,120,120))

add_page_break(doc)

# ── SECTION 1 — MASTER SUMMARY TABLE ─────────────────────────────────────────
p = doc.add_paragraph()
add_run(p, "1. Annual Summary — Combined Returns vs Nifty", bold=True, size=14, color=(27,58,92))
doc.add_paragraph()

p = doc.add_paragraph()
add_run(p, "Each row shows the equal-weighted average return of the top 40 and bottom 10 stocks held "
           "during IN months that year, compared against Nifty 50. "
           "'Alpha' = Top40 avg − Nifty. 'Drag' = Bottom10 avg − Nifty.", size=9, color=(80,80,80))
doc.add_paragraph()

cols = ["Year", "Nifty Return", "Top 40 Avg Return", "vs Nifty", "Bottom 10 Avg Return", "vs Nifty", "Stocks Held"]
tbl2 = doc.add_table(rows=1+len(yearly_data), cols=len(cols))
tbl2.style = "Table Grid"
tbl2.alignment = WD_TABLE_ALIGNMENT.CENTER

for j, col in enumerate(cols):
    c = tbl2.rows[0].cells[j]
    set_cell_bg(c, DARK_BLUE)
    p = c.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_run(p, col, bold=True, size=9, color=(255,255,255))

for i, (yr, d) in enumerate(sorted(yearly_data.items())):
    bg       = ALT if i % 2 == 0 else WHITE
    t40      = d["top40_avg"]
    b10      = d["bottom10_avg"]
    nifty    = d["nifty_ret"]
    alpha    = t40 - nifty if not np.isnan(nifty) else np.nan
    drag     = b10 - nifty if not np.isnan(nifty) else np.nan
    t40_bg   = GREEN_BG if t40 >= nifty else RED_BG
    b10_bg   = GREEN_BG if b10 >= nifty else RED_BG

    row_vals = [
        (str(yr),                                  bg,    False, (30,30,30)),
        (f"{nifty:+.1f}%",                         bg,    True,  (27,58,92)),
        (f"{t40:+.1f}%",                           t40_bg, True, (0,100,0) if t40>=0 else (180,0,0)),
        (f"{alpha:+.1f}%",                         t40_bg, False,(0,120,0) if alpha>=0 else (180,0,0)),
        (f"{b10:+.1f}%",                           b10_bg, True, (0,100,0) if b10>=0 else (180,0,0)),
        (f"{drag:+.1f}%",                          b10_bg, False,(0,120,0) if drag>=0 else (180,0,0)),
        (str(d["n_stocks"]),                        bg,    False, (80,80,80)),
    ]
    for j, (val, cbg, bld, clr) in enumerate(row_vals):
        cell = tbl2.rows[i+1].cells[j]
        set_cell_bg(cell, cbg)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        add_run(p, val, bold=bld, size=9, color=clr)

add_page_break(doc)

# ── SECTION 2 — YEAR-BY-YEAR DETAIL ──────────────────────────────────────────
p = doc.add_paragraph()
add_run(p, "2. Year-by-Year Detail — All Stocks Ranked", bold=True, size=14, color=(27,58,92))

p = doc.add_paragraph()
add_run(p, "For each year: Top 40 stocks (best performers) shown first, then Bottom 10 (worst performers). "
           "Returns are full calendar-year price returns. All stocks were selected in at least one IN-month rebalance during that year.",
           size=9, color=(80,80,80))

for yr, d in sorted(yearly_data.items()):
    doc.add_paragraph()

    # year header band
    tbl_h = doc.add_table(rows=1, cols=4)
    tbl_h.style = "Table Grid"
    tbl_h.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr_data = [
        (f"{yr}", DARK_BLUE),
        (f"Nifty: {d['nifty_ret']:+.1f}%", MID_BLUE),
        (f"Top 40 avg: {d['top40_avg']:+.1f}%", "1A6B3A" if d['top40_avg'] >= 0 else "8B0000"),
        (f"Bottom 10 avg: {d['bottom10_avg']:+.1f}%", "8B0000" if d['bottom10_avg'] < d['nifty_ret'] else "1A6B3A"),
    ]
    for j, (txt, bg) in enumerate(hdr_data):
        set_cell_bg(tbl_h.rows[0].cells[j], bg)
        p = tbl_h.rows[0].cells[j].paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        add_run(p, txt, bold=True, size=10, color=(255,255,255))

    # combined table: top 40 + bottom 10
    all_stocks = list(d["top40"]) + [("--- BOTTOM 10 ---", None)] + list(d["bottom10"])
    col_hdrs = ["Rank", "Symbol", "Annual Return", "vs Nifty", "Group"]
    tbl_s = doc.add_table(rows=1+len(all_stocks), cols=len(col_hdrs))
    tbl_s.style = "Table Grid"

    for j, ch in enumerate(col_hdrs):
        c = tbl_s.rows[0].cells[j]
        set_cell_bg(c, MID_BLUE)
        p = c.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        add_run(p, ch, bold=True, size=8, color=(255,255,255))

    rank = 0
    for ri, item in enumerate(all_stocks):
        sym, ret = item
        row = tbl_s.rows[ri+1]

        if sym == "--- BOTTOM 10 ---":
            # divider row
            for j in range(len(col_hdrs)):
                set_cell_bg(row.cells[j], "2E2E2E")
                p = row.cells[j].paragraphs[0]
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                if j == 2:
                    add_run(p, "▼  BOTTOM 10 PERFORMERS  ▼", bold=True, size=8, color=(255,220,0))
            rank = 41
            continue

        rank += 1
        is_top = rank <= 40
        group  = "Top 40" if is_top else "Bottom 10"
        vs_n   = ret - d["nifty_ret"] if not np.isnan(d["nifty_ret"]) else np.nan

        if is_top:
            bg     = "E8F5E9" if ri % 2 == 0 else "F1F8F2"
            grp_bg = "1A6B3A"
        else:
            bg     = "FFEBEE" if ri % 2 == 0 else "FFF0F0"
            grp_bg = "8B0000"

        ret_clr  = (0,120,0) if ret >= 0 else (180,0,0)
        vsn_clr  = (0,120,0) if (not np.isnan(vs_n) and vs_n >= 0) else (180,0,0)

        vals = [
            (str(rank),                              bg,    False, (80,80,80)),
            (sym.replace("-EQ",""),                  bg,    True,  (27,58,92)),
            (f"{ret:+.1f}%",                         bg,    True,  ret_clr),
            (f"{vs_n:+.1f}%" if not np.isnan(vs_n) else "—", bg, False, vsn_clr),
            (group,                                  grp_bg, True, (255,255,255)),
        ]
        for j, (val, cbg, bld, clr) in enumerate(vals):
            cell = row.cells[j]
            set_cell_bg(cell, cbg)
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            add_run(p, val, bold=bld, size=8, color=clr)

    if yr != max(yearly_data.keys()):
        add_page_break(doc)

# ── save ──────────────────────────────────────────────────────────────────────
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
doc.save(str(OUT_PATH))
print(f"\nDOCX saved → {OUT_PATH}")
