import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mt
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                Image, PageBreak, KeepTogether)

HERE = Path(__file__).parent
RES  = HERE / "results"
FIG  = RES / "report_figs"
FIG.mkdir(exist_ok=True)
OUT  = RES / "Open_High_Short_Backtest_Report.pdf"
sys.path.insert(0, str(HERE))
import run_backtest_oh_short_v4 as v4

CAPITAL = 1_000_000
SLIP    = 0.0005
BLUE, RED, ORANGE = "#2a78d6", "#e34948", "#eb6834"
INK, INK2, GRIDC  = "#0b0b0b", "#52514e", "#e4e3df"
NEG_TXT = colors.HexColor("#b42318")


# ── helpers ──────────────────────────────────────────────────────────────────
def trade_cost(pos):
    pos = np.asarray(pos, dtype=float)
    exch = 0.0000297 * 2 * pos
    return 0.00025 * pos + exch + 0.00003 * pos + 40 + 0.18 * (exch + 40)


def inr(x, sign=True):
    neg = x < 0
    s = f"{abs(round(x)):d}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    pre = "-" if neg else ("+" if sign and x > 0 else "")
    return f"{pre}Rs {s}"


def lakh(x):
    return f"{'-' if x < 0 else '+'}Rs {abs(x)/1e5:.2f} L"


def stats(df, net_col, date_col, hold_col=None):
    d = df.copy()
    d["_d"] = pd.to_datetime(d[date_col]).dt.normalize()
    daily = d.groupby("_d")[net_col].sum().sort_index()
    eq = CAPITAL + daily.cumsum()
    curve = pd.concat([pd.Series([CAPITAL]), eq.reset_index(drop=True)])
    dd = ((curve - curve.cummax()) / curve.cummax() * 100).min()
    w, l = d[d[net_col] > 0], d[d[net_col] <= 0]
    pf = w[net_col].sum() / abs(l[net_col].sum()) if l[net_col].sum() else np.inf
    months = d.groupby(d["_d"].dt.to_period("M"))[net_col].agg(["sum", "count"])
    exits = d.groupby("exit_reason")[net_col].agg(["count", "sum"]) if "exit_reason" in d else None
    days = d["_d"].nunique()
    return {
        "trades": len(d), "days": days, "per_day": len(d) / days,
        "net": d[net_col].sum(), "ret": d[net_col].sum() / CAPITAL * 100, "dd": dd,
        "win": len(w) / len(d) * 100, "pf": pf, "avg": d[net_col].mean(),
        "avg_win": w[net_col].mean() if len(w) else 0, "avg_loss": l[net_col].mean() if len(l) else 0,
        "green": f"{(months['sum'] > 0).sum()} of {len(months)}",
        "eq": eq, "months": months, "exits": exits,
        "hold": d[hold_col].mean() * 5 if hold_col and hold_col in d else None,
        "start": d["_d"].min(), "end": d["_d"].max(),
    }


# ── charts ───────────────────────────────────────────────────────────────────
plt.rcParams.update({"font.size": 8.5, "axes.edgecolor": GRIDC, "axes.labelcolor": INK2,
                     "xtick.color": INK2, "ytick.color": INK2, "axes.titlesize": 9.5,
                     "axes.titleweight": "bold", "axes.titlecolor": INK})


def _style(ax):
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color=GRIDC, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def chart_equity(panels, fname, height=2.4):
    fig, axes = plt.subplots(1, len(panels), figsize=(7.2, height), squeeze=False)
    for ax, (title, series) in zip(axes[0], panels):
        _style(ax)
        for label, s, col in series:
            x = [s.index[0] - pd.Timedelta(days=1)] + list(s.index)
            y = [CAPITAL] + list(s.values)
            ax.plot(x, y, color=col, linewidth=1.6, label=label)
        ax.axhline(CAPITAL, color=INK2, linewidth=0.8, linestyle=(0, (3, 3)))
        ax.yaxis.set_major_formatter(mt.FuncFormatter(lambda v, _: f"{v/1e5:.1f}L"))
        ax.set_title(title, loc="left")
        ax.xaxis.set_major_locator(matplotlib.dates.AutoDateLocator(maxticks=6))
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%b %y"))
        if len(series) > 1:
            ax.legend(frameon=False, loc="upper left", fontsize=7.5)
    fig.tight_layout()
    fig.savefig(FIG / fname, dpi=200)
    plt.close(fig)
    return FIG / fname


def chart_months(panels, fname, height=2.3):
    fig, axes = plt.subplots(1, len(panels), figsize=(7.2, height), squeeze=False)
    for ax, (title, months) in zip(axes[0], panels):
        _style(ax)
        vals = months["sum"].values
        labels = [p.strftime("%b %y") for p in months.index]
        ax.bar(range(len(vals)), vals, color=[BLUE if v > 0 else RED for v in vals],
               width=0.72, edgecolor="white", linewidth=1)
        ax.axhline(0, color=INK2, linewidth=0.8)
        step = max(1, int(np.ceil(len(labels) / (7 if len(panels) == 1 else 4))))
        ax.set_xticks(range(0, len(labels), step))
        ax.set_xticklabels(labels[::step])
        ax.yaxis.set_major_formatter(mt.FuncFormatter(lambda v, _: f"{v/1000:.0f}k"))
        ax.set_title(title, loc="left")
    fig.tight_layout()
    fig.savefig(FIG / fname, dpi=200)
    plt.close(fig)
    return FIG / fname


def chart_summary(rows, fname):
    labels = [r[0] for r in rows][::-1]
    vals = [r[1] for r in rows][::-1]
    fig, ax = plt.subplots(figsize=(7.2, 0.32 * len(rows) + 0.7))
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="x", color=GRIDC, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)
    ax.barh(range(len(vals)), vals, color=[BLUE if v > 0 else RED for v in vals], height=0.62,
            edgecolor="white", linewidth=1)
    ax.axvline(0, color=INK2, linewidth=0.8)
    ax.set_yticks(range(len(vals)))
    ax.set_yticklabels(labels, fontsize=7.8)
    span = max(abs(v) for v in vals)
    for i, v in enumerate(vals):
        ax.text(v + (span * 0.015 if v >= 0 else -span * 0.015), i, lakh(v),
                va="center", ha="left" if v >= 0 else "right", fontsize=7.3, color=INK)
    ax.set_xlim(min(vals) - span * 0.35, max(max(vals), 0) + span * 0.35)
    ax.xaxis.set_major_formatter(mt.FuncFormatter(lambda v, _: f"{v/1e5:.0f}L"))
    ax.set_xlabel("Net profit / loss after costs (Rs lakh)")
    fig.tight_layout()
    fig.savefig(FIG / fname, dpi=200)
    plt.close(fig)
    return FIG / fname


# ── load / compute every test ────────────────────────────────────────────────
print("Computing results ...")
T = {}

t1 = pd.read_csv(RES / "oh_short_trades.csv")
t1["pos_val"] = t1["qty"] * t1["entry"]
t1["cost"] = trade_cost(t1["pos_val"])
t1["net"] = t1["pnl"] - t1["cost"]
T["t1_gross"] = stats(t1, "pnl", "entry_dt", "holding_bars")
T["t1_net"] = stats(t1, "net", "entry_dt", "holding_bars")
T["t1_cost"] = t1["cost"].sum()

t2 = pd.read_csv(RES / "oh_short_v2_trades.csv")
t2["pos_val"] = t2["qty"] * t2["entry"]
t2["cost"] = trade_cost(t2["pos_val"])
t2["net"] = t2["pnl"] - t2["cost"]
T["t2_gross"] = stats(t2, "pnl", "entry_dt", "holding_bars")
T["t2_net"] = stats(t2, "net", "entry_dt", "holding_bars")
T["t2_cost"] = t2["cost"].sum()
t2_day_exp = t2.groupby(pd.to_datetime(t2["entry_dt"]).dt.date)["pos_val"].sum()

g3_is = pd.read_csv(RES / "oh_short_v3_grid.csv")
g3_oos = pd.read_csv(RES / "oh_short_v3_oos_grid.csv")
t3_is = pd.read_csv(RES / "oh_short_v3_best_trades.csv")
t3_oos = pd.read_csv(RES / "oh_short_v3_oos_trades.csv")
T["t3_is"] = stats(t3_is, "net", "date")
T["t3_oos"] = stats(t3_oos, "net", "date")
t3_oos["net_slip"] = t3_oos["net"] - t3_oos["pos_val"] * SLIP
T["t3_oos_slip"] = t3_oos["net_slip"].sum()
big = t3_oos[t3_oos["pos_val"] > 1_000_000]
T["t3_big_n"], T["t3_big_net"] = len(big), big["net"].sum()
T["t3_small_net"] = t3_oos[t3_oos["pos_val"] <= 1_000_000]["net"].sum()
apollo = t3_oos.loc[t3_oos["pos_val"].idxmax()]

print("Re-running v4 (opening-range, wide stops) ...")
nifty = v4.nifty_days()
rows = []
for d in sorted(v4.DATA_DIR.iterdir()):
    if d.is_dir() and d.name.startswith("NSE_") and d.name.endswith("_EQ"):
        df = v4.load_5min(d)
        if df is not None:
            rows.extend(v4.scan_stock(d.name, df, nifty))
sig = pd.DataFrame(rows)
fno_sig = sig[~sig["symbol"].isin({"BRIGADE", "JSWENERGY", "MMTC"})]
V4 = {}
for univ, s in (("all", sig), ("fno", fno_sig)):
    for ver in v4.VERSIONS:
        for per_name, per in (("is", v4.IN_SAMPLE), ("oos", v4.OUT_OF_SAMPLE)):
            res = v4.run(s, ver, per)
            tr = res["trades"]
            V4[(univ, ver, per_name)] = {
                "st": stats(tr, "net", "date"),
                "gross": tr["gross"].sum(), "costs": tr["cost"].sum(), "slip": tr["slip"].sum(),
                "lev": tr.groupby("date")["pos_val"].sum().max() / CAPITAL,
            }
n_fno = fno_sig["symbol"].nunique()

# ── figures ──────────────────────────────────────────────────────────────────
summary_rows = [
    ("2. 5 trades/day, Jan-Jun 2026",              T["t2_net"]["net"]),
    ("3. Best of 27 combos, Jan-Jun 2026",         T["t3_is"]["net"]),
    ("4. Same rule on May 2024-Dec 2025",          T["t3_oos"]["net"]),
    ("5A. Opening range, stop 0.5% (24-25)",       V4[("all", "A", "is")]["st"]["net"]),
    ("5B. + below prev-day low (24-25)",           V4[("all", "B", "is")]["st"]["net"]),
    ("5C. Opening range, stop 1% (24-25)",         V4[("all", "C", "is")]["st"]["net"]),
    ("5B. on Jan-Jun 2026 (final check)",          V4[("all", "B", "oos")]["st"]["net"]),
]
f_summary = chart_summary(summary_rows, "summary.png")
f_t1 = chart_equity([("Test 1 - account value, before vs after costs",
                      [("Before costs", T["t1_gross"]["eq"], BLUE), ("After costs", T["t1_net"]["eq"], ORANGE)])],
                    "t1_eq.png")
f_t2 = chart_equity([("Test 2 - account value, before vs after costs",
                      [("Before costs", T["t2_gross"]["eq"], BLUE), ("After costs", T["t2_net"]["eq"], ORANGE)])],
                    "t2_eq.png")
f_t2m = chart_months([("Test 2 - monthly profit/loss after costs", T["t2_net"]["months"])], "t2_m.png")
f_t3 = chart_equity([("Picked period: Jan-Jun 2026", [("", T["t3_is"]["eq"], BLUE)]),
                     ("New period: May 2024-Dec 2025", [("", T["t3_oos"]["eq"], BLUE)])], "t3_eq.png")
f_t3m = chart_months([("Monthly P&L on the new period (after costs)", T["t3_oos"]["months"])], "t3_m.png")
b_is, b_oos = V4[("all", "B", "is")]["st"], V4[("all", "B", "oos")]["st"]
f_t5 = chart_equity([("Version B: May 2024-Dec 2025", [("", b_is["eq"], BLUE)]),
                     ("Version B: Jan-Jun 2026 (final check)", [("", b_oos["eq"], BLUE)])], "t5_eq.png")
f_t5m = chart_months([("Version B monthly P&L, May 2024-Dec 2025", b_is["months"]),
                      ("Jan-Jun 2026", b_oos["months"])], "t5_m.png")

# ── PDF building blocks ──────────────────────────────────────────────────────
ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Heading1"], fontSize=15, spaceBefore=4, spaceAfter=6,
                    textColor=colors.HexColor(INK))
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=11.5, spaceBefore=8, spaceAfter=4,
                    textColor=colors.HexColor(INK))
BODY = ParagraphStyle("B", parent=ss["BodyText"], fontSize=9.2, leading=12.6,
                      textColor=colors.HexColor(INK), spaceAfter=4)
SMALL = ParagraphStyle("S", parent=BODY, fontSize=8, leading=10.5, textColor=colors.HexColor(INK2))
CELL = ParagraphStyle("C", parent=BODY, fontSize=8, leading=10, spaceAfter=0)
CELLB = ParagraphStyle("CB", parent=CELL, fontName="Helvetica-Bold")
BUL = ParagraphStyle("BL", parent=BODY, leftIndent=12, bulletIndent=3, spaceAfter=2)
W = A4[0] - 3.6 * cm


def P(t, st=BODY):
    return Paragraph(t, st)


def bullets(items):
    return [Paragraph(t, BUL, bulletText="•") for t in items]


def img(path, width=W):
    from reportlab.lib.utils import ImageReader
    iw, ih = ImageReader(str(path)).getSize()
    return Image(str(path), width=width, height=width * ih / iw)


def table(data, widths, num_cols=(), color_cols=(), header=True, zebra=True, font=8):
    rows = []
    for r, row in enumerate(data):
        cells = []
        for c, v in enumerate(row):
            if isinstance(v, Paragraph):
                cells.append(v); continue
            st = CELLB if (header and r == 0) else CELL
            st = ParagraphStyle("f", parent=st, fontSize=font, leading=font + 2)
            if c in num_cols and not (header and r == 0):
                st = ParagraphStyle("n", parent=st, alignment=2)
            txt = str(v).replace("&amp;", "&").replace("&", "&amp;")
            if c in color_cols and not (header and r == 0) and txt.lstrip().startswith("-"):
                txt = f'<font color="#b42318">{txt}</font>'
            cells.append(Paragraph(txt, st))
        rows.append(cells)
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#9a9993")),
             ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#c9c8c2")),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0efec")))
    if zebra:
        for r in range(2 if header else 1, len(rows), 2):
            style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#f8f8f6")))
    t.setStyle(TableStyle(style))
    return t


def kv_table(pairs, cols=2):
    data, row = [], []
    for k, v in pairs:
        row += [Paragraph(k, ParagraphStyle("k", parent=CELL, textColor=colors.HexColor(INK2))),
                Paragraph(f"<b>{v}</b>" if not str(v).startswith("-") else
                          f'<b><font color="#b42318">{v}</font></b>', ParagraphStyle("v", parent=CELL, alignment=2))]
        if len(row) == cols * 2:
            data.append(row); row = []
    if row:
        data.append(row + [""] * (cols * 2 - len(row)))
    cw = W / cols
    t = Table(data, colWidths=[cw * 0.58, cw * 0.42] * cols)
    st = [("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
          ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e4e3df"))]
    for c in range(1, cols):
        st.append(("LEFTPADDING", (c * 2, 0), (c * 2, -1), 14))
    t.setStyle(TableStyle(st))
    return t


def stat_pairs(s, extra=()):
    pairs = [("Trades", f"{s['trades']:,}"), ("Trading days", f"{s['days']}"),
             ("Net profit / loss", inr(s["net"])), ("Return on Rs 10 L", f"{s['ret']:+.1f}%"),
             ("Win rate", f"{s['win']:.1f}%"), ("Profit factor", f"{s['pf']:.2f}"),
             ("Average per trade", inr(s["avg"])), ("Max drawdown", f"{s['dd']:.1f}%"),
             ("Average win", inr(s["avg_win"])), ("Average loss", inr(s["avg_loss"])),
             ("Months in profit", s["green"])]
    if s.get("hold"):
        pairs.append(("Average holding time", f"{s['hold']:.0f} min"))
    return pairs + list(extra)


def exits_table(s, net_label="Net P&L"):
    data = [["Exit", "Trades", "Share", net_label]]
    names = {"sl": "Stop-loss hit", "target": "Target hit", "eod": "Closed at 15:15"}
    for k, r in s["exits"].iterrows():
        data.append([names.get(k, k), f"{int(r['count']):,}", f"{r['count']/s['trades']*100:.1f}%", inr(r["sum"])])
    return table(data, [W * 0.4, W * 0.18, W * 0.17, W * 0.25], num_cols=(1, 2, 3), color_cols=(3,))


def verdict_box(text, bg="#fdecea", border="#d03b3b"):
    t = Table([[Paragraph(text, ParagraphStyle("vb", parent=BODY, fontSize=10, leading=14))]], colWidths=[W])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg)),
                           ("BOX", (0, 0), (-1, -1), 1, colors.HexColor(border)),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return t


def note_box(text):
    return verdict_box(text, bg="#f4f7fc", border="#86b6ef")


# ── story ────────────────────────────────────────────────────────────────────
story = []
s1g, s1n = T["t1_gross"], T["t1_net"]
s2g, s2n = T["t2_gross"], T["t2_net"]
s3i, s3o = T["t3_is"], T["t3_oos"]

story += [
    P("Open = High Short Strategy", ParagraphStyle("T", parent=H1, fontSize=22, leading=26)),
    P("Backtest report  |  Nifty F&amp;O stocks  |  5-minute candles  |  May 2024 to June 2026", SMALL),
    P("Starting capital Rs 10,00,000  |  Prepared 31 Oct 2026", SMALL),
    Spacer(1, 10),
    verdict_box("<b>VERDICT: DO NOT TRADE.</b> Six rounds of testing and more than 30 variations "
                "were run. None makes money reliably after brokerage, taxes and slippage. The versions that "
                "looked profitable on one period lost money on a different period - the profit was luck, "
                "not an edge."),
    Spacer(1, 10),
    P("The idea in one line", H2),
    P("When a stock's candle opens at its highest price (Open = High), buyers could not push it any "
      "higher, so sellers look in control. The strategy shorts the stock, with a stop-loss just above "
      "that high, and closes the trade the same day."),
    P("All results at a glance", H2),
    img(f_summary),
    P("Blue = profit, red = loss. All figures are after brokerage and taxes; the Test 5 bars also include "
      "0.05% slippage. Test 1 is left out of the chart: it is not executable and its "
      f"{lakh(T['t1_net']['net'])} loss would squash every other bar.", SMALL),
]

summary_tbl = [["#", "What was tested", "Period", "Trades", "Net P&L", "Result"],
    ["1", "Every signal, 1% risk per trade", "Jan-Jun 26", f"{s1n['trades']:,}", inr(s1n["net"]), "Loss - not executable"],
    ["2", "5 trades a day, 1% risk per day", "Jan-Jun 26", f"{s2n['trades']}", inr(s2n["net"]), "Loss after costs"],
    ["3", "Best of 27 combinations (stock pick, target, Nifty filter)", "Jan-Jun 26", f"{s3i['trades']}", inr(s3i["net"]), "Profit - but picked after the fact"],
    ["4", "Test 3's winner on new data", "May 24-Dec 25", f"{s3o['trades']}", inr(s3o["net"]), "Failed"],
    ["5", "Opening range + wider stops (3 versions, best shown)", "May 24-Dec 25", f"{V4[('all','B','is')]['st']['trades']}",
     inr(V4[("all", "B", "is")]["st"]["net"]), "Loss"],
    ["5", "Version B final check", "Jan-Jun 26", f"{b_oos['trades']}", inr(b_oos["net"]), "Loss"],
    ["6", f"Test 5 on F&amp;O-only list ({n_fno} stocks), best result", "Jan-Jun 26",
     f"{V4[('fno','A','oos')]['st']['trades']}", inr(V4[("fno", "A", "oos")]["st"]["net"]),
     "Break-even, lost Rs 2.5 L before"],
]
story += [Spacer(1, 6), table(summary_tbl, [W*0.04, W*0.38, W*0.14, W*0.09, W*0.15, W*0.20],
                              num_cols=(3, 4), color_cols=(4,)), PageBreak()]

# ── how to read ──────────────────────────────────────────────────────────────
story += [P("How to read this report", H1),
          table([["Term", "What it means in plain words"],
                 ["Net P&amp;L", "Profit or loss after all charges. This is the number that matters."],
                 ["Gross P&amp;L", "Profit or loss before charges. Looks better than reality."],
                 ["Win rate", "Share of trades that made money. Can be under 50% and still profitable if wins are bigger than losses."],
                 ["Profit factor (PF)", "Total profits divided by total losses. Above 1 = makes money, 1.0 = break-even, "
                  "below 1 = loses. A strategy worth trading is usually 1.3 or higher."],
                 ["Average per trade", "Net P&amp;L divided by number of trades. Must clearly beat the charges per trade."],
                 ["Max drawdown", "The biggest fall in account value from a high point. -10% means the account dropped "
                  "10% from its best level at some point."],
                 ["Months in profit", "How many calendar months ended positive."],
                 ["Leverage", "Total value of positions held on the busiest day divided by Rs 10 L capital. "
                  "Intraday (MIS) margin allows up to about 5x, less for many stocks."],
                 ["Picked period / new period", "Settings are chosen on one stretch of data (picked period, or "
                  "'in-sample'). The real test is running the same settings, unchanged, on data they never saw "
                  "(new period, or 'out-of-sample')."],
                 ["Slippage", "Getting a slightly worse price than the chart shows. Assumed 0.05% of the trade value "
                  "per round trip where stated."]],
                [W * 0.24, W * 0.76], zebra=True),
          Spacer(1, 8),
          P("Data and assumptions", H1),
          *bullets([
              "<b>Stocks:</b> 182 NSE F&amp;O stocks (list from 2024). Test 6 uses only the 179 still on the 2026 F&amp;O list.",
              "<b>Candles:</b> 5-minute OHLCV from Fyers, 28 May 2024 to 25 Jun 2026.",
              "<b>Capital:</b> Rs 10,00,000. Intraday shorts only - every trade is closed the same day.",
              "<b>Charges per trade:</b> brokerage Rs 20 per order (Rs 40 round trip), STT 0.025% on the sell side, "
              "exchange charges 0.00297% each side, stamp duty 0.003%, GST 18% on brokerage and exchange charges. "
              "On a typical Rs 2-3 L trade this is about Rs 120-170.",
              "<b>Entry:</b> at the open of the candle after the signal candle (what you could actually get).",
              "<b>Stop and target:</b> if a candle touches both, the stop is assumed to hit first (the cautious choice).",
              "<b>Not included:</b> per-stock margin limits, short-sale bans, circuit limits, taxes on profit.",
          ]),
          PageBreak()]

# ── test 1 ───────────────────────────────────────────────────────────────────
story += [P("Test 1 - Take every signal", H1),
          P("<b>Rules:</b> any 5-minute candle with Open = High (within 0.1%) and a red close. Short at the next "
            "candle's open. Stop 0.1% above the high, target 2x the risk, else close at 15:15. First signal per "
            "stock per day only. Risk 1% of capital (Rs 10,000) per trade, position capped at Rs 2 L."),
          P("<b>Period:</b> 1 Jan to 30 Jun 2026."),
          kv_table([("Before costs", inr(s1g["net"])), ("Charges", inr(-T["t1_cost"])),
                    ("After costs", inr(s1n["net"])), ("Trades per day", f"{s1n['per_day']:.0f}"),
                    ("Win rate (before costs)", f"{s1g['win']:.1f}%"), ("Profit factor (before costs)", f"{s1g['pf']:.2f}"),
                    ("Profit factor (after costs)", f"{s1n['pf']:.2f}"),
                    ("Lowest account value (after costs)", inr(s1n["eq"].min(), sign=False)),
                    ("Average per trade (before costs)", inr(s1g["avg"])), ("Average holding time", f"{s1g['hold']:.0f} min")]),
          Spacer(1, 6), exits_table(s1g, "P&amp;L before costs"), Spacer(1, 6), img(f_t1),
          note_box(f"<b>What it means:</b> about {s1n['per_day']:.0f} trades a day is impossible with Rs 10 L, so this "
                   f"only measures the raw signal. Before charges it made {lakh(s1g['net'])}; charges of "
                   f"{lakh(-T['t1_cost'])} turn that into {lakh(s1n['net'])} - the account would have gone below zero. "
                   "The signal earns less per trade than it costs to trade."),
          PageBreak()]

# ── test 2 ───────────────────────────────────────────────────────────────────
story += [P("Test 2 - 5 trades a day, 1% risk per day", H1),
          P("<b>Rules:</b> same signal and exits as Test 1. Only the first 5 signals of the day are taken. "
            "Total risk per day = 1% of capital, split equally (about Rs 2,000 per trade), compounding."),
          P("<b>Period:</b> 1 Jan to 30 Jun 2026."),
          kv_table(stat_pairs(s2n, [("Before costs", inr(s2g["net"])), ("Charges", inr(-T["t2_cost"])),
                                     ("Avg position size", inr(t2["pos_val"].mean(), sign=False)),
                                     ("Busiest day exposure", f"{t2_day_exp.max()/CAPITAL:.1f}x capital")])),
          Spacer(1, 6), exits_table(s2n), Spacer(1, 6), img(f_t2), img(f_t2m),
          note_box(f"<b>What it means:</b> before costs it made {lakh(s2g['net'])} with a {s2g['dd']:.1f}% drawdown, which "
                   f"looked promising. But each trade averaged about {inr(t2['cost'].mean(), sign=False)} in charges against "
                   f"{inr(s2g['avg'])} average profit, so the net result is {lakh(s2n['net'])}. Also, every trade entered at "
                   f"09:20 - all signals came from the first candle, so 'first 5' was really just the first 5 in file order."),
          PageBreak()]

# ── test 3 ───────────────────────────────────────────────────────────────────
def grid_rows(g):
    names = {"file": "File order", "body": "Biggest red candle", "gap": "Biggest gap down"}
    nf = {"none": "None", "nifty_red_open": "Nifty 1st candle red", "nifty_gap_down": "Nifty gap down"}
    data = [["Stock pick", "Target", "Nifty filter", "Trades", "Net P&L", "Max DD", "Win %", "PF"]]
    for _, r in g.iterrows():
        data.append([names[r["ranking"]], r["target"], nf[r["nifty"]], f"{int(r['trades'])}",
                     inr(r["net"]), f"{r['max_dd']:.1f}%", f"{r['win_rate']:.1f}%", f"{r['pf']:.2f}"])
    return data


gw = [W*0.2, W*0.09, W*0.2, W*0.09, W*0.15, W*0.1, W*0.09, W*0.08]
story += [P("Test 3 - Fixing the three weak points (27 combinations)", H1),
          P("<b>Rules:</b> as Test 2 (5 trades a day, 1% risk per day), after charges. Three things were varied: "
            "<b>how the 5 stocks are picked</b> (file order, biggest red first candle, biggest gap down), "
            "<b>target</b> (2x risk, 3x risk, or none - hold to stop or 15:15) and a <b>Nifty filter</b> "
            "(none, Nifty's first candle red, Nifty gapped down). 3 x 3 x 3 = 27 combinations."),
          P("<b>Period:</b> 1 Jan to 30 Jun 2026. Sorted best to worst."),
          table(grid_rows(g3_is), gw, num_cols=(3, 4, 5, 6, 7), color_cols=(4, 5)),
          Spacer(1, 6),
          note_box(f"<b>Best combination:</b> biggest gap down + no target + Nifty gap-down filter: "
                   f"{lakh(s3i['net'])}, max drawdown {s3i['dd']:.1f}%, profit factor {s3i['pf']:.2f}, "
                   f"{s3i['green']} months in profit. Only {(g3_is['net'] > 0).sum()} of 27 combinations made money. "
                   "Picking the best of 27 on just 6 months is likely to find luck, so it had to be checked on new data (Test 4)."),
          PageBreak()]

# ── test 4 ───────────────────────────────────────────────────────────────────
story += [P("Test 4 - Test 3's winner on new data", H1),
          P("<b>Rules:</b> exactly Test 3's best combination, unchanged. <b>Period:</b> 29 May 2024 to 31 Dec 2025 "
            "(data the rule never saw)."),
          kv_table(stat_pairs(s3o, [("After 0.05% slippage", inr(T["t3_oos_slip"]))])),
          Spacer(1, 6), img(f_t3), img(f_t3m),
          P("Same 27 combinations on the new period", H2),
          table(grid_rows(g3_oos), gw, num_cols=(3, 4, 5, 6, 7), color_cols=(4, 5)),
          Spacer(1, 6),
          note_box(f"<b>What it means:</b> the winning rule ranked "
                   f"{int(g3_oos.reset_index(drop=True).index[(g3_oos['ranking']=='gap') & (g3_oos['target']=='none') & (g3_oos['nifty']=='nifty_gap_down')][0]) + 1}"
                   f" of 27 on new data and lost money ({lakh(s3o['net'])}, or {lakh(T['t3_oos_slip'])} after slippage). "
                   f"Only {(g3_oos['net'] > 0).sum()} of 27 made money, and a different set came out on top - a sign the "
                   "earlier winner was luck.<br/><br/><b>Data note:</b> on "
                   f"{pd.to_datetime(apollo['date']).strftime('%d %b %Y')} one {apollo['symbol']} trade had a stop only "
                   f"{apollo['sl_dist']*100:.1f} paise away, so the sizing formula bought {inr(apollo['pos_val'], sign=False)} of stock. "
                   f"{T['t3_big_n']} trades were over Rs 10 L. Without them the 19-month result is {inr(T['t3_small_net'])} - "
                   "still break-even at best. Test 5 adds a minimum stop distance and a position cap to stop this."),
          PageBreak()]

# ── test 5 ───────────────────────────────────────────────────────────────────
def v4_table(univ):
    data = [["Ver", "Period", "Trades", "Before costs", "Charges", "Slippage", "Net P&L", "Max DD", "PF", "Lev."]]
    for ver in v4.VERSIONS:
        for per_name, lbl in (("is", "May 24-Dec 25"), ("oos", "Jan-Jun 26")):
            r = V4[(univ, ver, per_name)]
            s = r["st"]
            data.append([ver, lbl, f"{s['trades']}", inr(r["gross"]), inr(-r["costs"]), inr(-r["slip"]),
                         inr(s["net"]), f"{s['dd']:.1f}%", f"{s['pf']:.2f}", f"{r['lev']:.1f}x"])
    return table(data, [W*0.05, W*0.15, W*0.08, W*0.12, W*0.12, W*0.12, W*0.13, W*0.08, W*0.06, W*0.06],
                 num_cols=tuple(range(2, 10)), color_cols=(3, 4, 5, 6, 7), font=7.4)


story += [P("Test 5 - Opening range with wider stops", H1),
          P("<b>Why:</b> the 0.1% stop made positions huge compared with the risk, so charges ate everything. "
            "A wider stop means a smaller position for the same Rs 2,000 risk, so charges become a small slice."),
          P("<b>Rules (shared, decided before running):</b>"),
          *bullets([
              "First 15 minutes (09:15-09:30) open at their high (within 0.1%) and close red.",
              "The stock gapped down from yesterday's close and is weaker than Nifty over those 15 minutes.",
              "Short at the 09:30 open (skip if price is already above the 15-minute high).",
              "Stop above the 15-minute high, but never closer than the minimum below. No target - close at 15:15.",
              "5 stocks a day, weakest versus Nifty first. 1% risk per day. Position cap Rs 5 L. "
              "Charges <b>and</b> 0.05% slippage included.",
          ]),
          table([["Version", "Difference"],
                 ["A", "Minimum stop 0.5%"],
                 ["B", "A, plus the 15-minute close must be below yesterday's low"],
                 ["C", "A, but minimum stop 1.0%"]], [W*0.15, W*0.85], zebra=False),
          P("<b>Method:</b> pick the best version on May 2024-Dec 2025, then run only that one on Jan-Jun 2026. "
            "Jan-Jun 2026 results for A and C are shown for completeness; they played no part in the choice.", SMALL),
          Spacer(1, 4), v4_table("all"), Spacer(1, 6),
          img(f_t5), img(f_t5m),
          note_box(f"<b>What it means:</b> the wider stop did its job - leverage fell to "
                   f"{V4[('all','C','is')]['lev']:.1f}-{V4[('all','A','is')]['lev']:.1f}x and charges shrank. But on May 2024-Dec 2025, "
                   "versions A and C make nothing even before charges, so there was no edge to save. Version B has a small "
                   f"edge before charges ({inr(V4[('all','B','is')]['gross'])} over 19 months), about half of what charges and "
                   f"slippage cost. It lost on both periods ({lakh(b_is['net'])} and {lakh(b_oos['net'])})."),
          Spacer(1, 12)]

# ── test 6 ───────────────────────────────────────────────────────────────────
story += [P("Test 6 - F&amp;O-only stocks", H1),
          P(f"<b>Rules:</b> Test 5, versions A, B and C, using only the {n_fno} stocks on the current F&amp;O list "
            "(BRIGADE, JSWENERGY and MMTC removed). All earlier tests already used F&amp;O stocks, so this changes little."),
          v4_table("fno"), Spacer(1, 6),
          note_box(f"<b>What it means:</b> almost identical to Test 5. Version A's small 2026 profit "
                   f"({inr(V4[('fno','A','oos')]['st']['net'])}, PF {V4[('fno','A','oos')]['st']['pf']:.2f}) follows a "
                   f"{lakh(V4[('fno','A','is')]['st']['net'])} loss on the earlier 19 months, so it is not an edge."),
          Spacer(1, 10),
          P("Conclusion", H1),
          *bullets([
              "<b>The signal is too weak.</b> After an Open = High candle, the price moves too little on average to pay "
              "Indian intraday charges (about Rs 120-170 per trade) plus slippage.",
              "<b>Every fix was tried:</b> fewer trades, smarter stock picking, wider and no targets, Nifty filters, "
              "opening-range version, wider stops, F&amp;O-only list. None survives an honest test on new data.",
              "<b>Good-looking results were luck.</b> The best of 27 combinations made +Rs 0.73 L on its picked period "
              "and lost on the new period. Testing many settings on short data will always find a 'winner'.",
              "<b>Recommendation:</b> do not trade this strategy and do not spend more time tuning it.",
          ]),
          P("Limitations", H2),
          *bullets([
              "Uses today's F&amp;O stock list for past periods - stocks that left F&amp;O are missing (survivorship).",
              "Charges are estimates of standard discount-broker rates; your broker may differ slightly.",
              "Per-stock intraday margin and short-sale restrictions are not modelled.",
              "Data ends 25 Jun 2026.",
          ]),
          Spacer(1, 8),
          P("Files: backtesting/open_high_short/ - run_backtest_oh_short.py (Test 1), _v2.py (Test 2), _v3.py (Test 3), "
            "_v3_oos.py (Test 4), _v4.py (Tests 5-6), build_report.py (this report). Trade lists in results/.", SMALL)]


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor(INK2))
    canvas.drawString(1.8 * cm, 1.1 * cm, "Open = High Short - backtest report")
    canvas.drawRightString(A4[0] - 1.8 * cm, 1.1 * cm, f"Page {doc.page}")
    canvas.restoreState()


doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=1.8*cm, rightMargin=1.8*cm,
                        topMargin=1.6*cm, bottomMargin=1.7*cm,
                        title="Open = High Short - Backtest Report", author="Backtest")
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print(f"Saved -> {OUT}")
