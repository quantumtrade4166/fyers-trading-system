"""
Interactive equity curve + drawdown for Momentum Weighted strategy
Uses plotly for interactive chart
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots

DATA_DIR       = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
LOOKBACK_DAYS  = 252
TOP_N          = 50
CAPITAL        = 1_000_000
SLIPPAGE_PCT   = 0.001
LIQUID_FUND_PA = 0.06
START_DATE     = "2006-01-01"
END_DATE       = "2026-06-18"

print("Downloading Nifty 50 (^NSEI)...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

print("Loading 500 symbols...")
frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]

prices = pd.DataFrame(frames).sort_index()
prices = prices.loc[START_DATE:END_DATE]
monthly_ends  = prices.resample("ME").last().index
monthly_rate  = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Price matrix: {prices.shape[0]} days x {prices.shape[1]} symbols\n")

print("Running backtest...")
portfolio_value = [CAPITAL]
dates           = [monthly_ends[0]]
cash_value      = CAPITAL
held_stocks     = {}
prev_date       = monthly_ends[0]
cash_periods    = []
in_cash         = False
cash_start      = None
monthly_data    = []  # for hover info

for i, rebal_date in enumerate(monthly_ends[1:], 1):
    idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
    if idx < 0:
        continue
    rebal_date     = prices.index[idx]
    current_px     = prices.iloc[idx]
    months_elapsed = (rebal_date - prev_date).days / 30.44
    cash_value    *= (1 + monthly_rate) ** months_elapsed

    nav = cash_value
    for sym, shares in held_stocks.items():
        p = current_px.get(sym, np.nan)
        if not pd.isna(p):
            nav += shares * p

    lb_idx = idx - LOOKBACK_DAYS
    if lb_idx < 0:
        portfolio_value.append(nav)
        dates.append(rebal_date)
        prev_date = rebal_date
        continue

    nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
    n_ma      = nifty_ma100.iloc[nifty_idx]
    n_px      = nifty.iloc[nifty_idx]
    market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

    # track cash periods
    if not market_up and not in_cash:
        in_cash    = True
        cash_start = rebal_date
    elif market_up and in_cash:
        in_cash = False
        cash_periods.append((cash_start, rebal_date))

    past_px     = prices.iloc[lb_idx]
    returns_12m = (current_px / past_px - 1).dropna()
    candidates  = returns_12m.nlargest(TOP_N).index.tolist() if market_up else []

    sell_value = cash_value
    for sym, shares in held_stocks.items():
        p = current_px.get(sym, np.nan)
        if not pd.isna(p):
            sell_value += shares * p * (1 - SLIPPAGE_PCT)

    held_stocks = {}
    cash_value  = sell_value

    top5_stocks = ""
    if candidates:
        raw   = {s: max(returns_12m[s], 0.001) for s in candidates}
        total = sum(raw.values())
        weights = {s: v/total for s, v in raw.items()}

        invested = 0
        for sym, w in weights.items():
            p = current_px.get(sym, np.nan)
            if pd.isna(p) or p <= 0:
                continue
            cost = sell_value * w * (1 + SLIPPAGE_PCT)
            held_stocks[sym] = cost / p
            invested += cost
        cash_value = max(sell_value - invested, 0)

        top5 = sorted(raw.items(), key=lambda x: x[1], reverse=True)[:5]
        top5_stocks = "<br>".join([f"{s}: {weights[s]*100:.1f}%" for s, _ in top5])

    monthly_data.append({
        "date":    rebal_date,
        "nav":     nav,
        "status":  "IN" if market_up else "OUT (Cash)",
        "top5":    top5_stocks if market_up else "Liquid Fund",
        "nifty":   float(n_px),
        "ma100":   float(n_ma) if not pd.isna(n_ma) else 0,
    })

    portfolio_value.append(nav)
    dates.append(rebal_date)
    prev_date = rebal_date

if in_cash:
    cash_periods.append((cash_start, prices.index[-1]))

final_nav = cash_value
for sym, shares in held_stocks.items():
    if sym in prices.columns:
        final_nav += shares * prices[sym].dropna().iloc[-1]
portfolio_value.append(final_nav)
dates.append(prices.index[-1])

nav_s = pd.Series(portfolio_value, index=dates)
nav_s = nav_s[~nav_s.index.duplicated(keep="last")]

# drawdown
dd_s = (nav_s - nav_s.cummax()) / nav_s.cummax() * 100

# Nifty normalised
nifty_plot = nifty.loc[START_DATE:END_DATE]
nifty_norm = (nifty_plot / nifty_plot.iloc[0]) * CAPITAL

# monthly hover data
md = pd.DataFrame(monthly_data).set_index("date")

print("Building interactive chart...")

fig = make_subplots(
    rows=3, cols=1,
    shared_xaxes=True,
    row_heights=[0.55, 0.25, 0.20],
    vertical_spacing=0.03,
    subplot_titles=["Portfolio NAV (Rs)", "Drawdown %", "Nifty 50 vs 100-day MA"]
)

# ── Cash period shading ────────────────────────────────────────────────────────
for start, end in cash_periods:
    for row in [1, 2, 3]:
        fig.add_vrect(
            x0=start, x1=end,
            fillcolor="rgba(255,80,80,0.10)",
            layer="below", line_width=0,
            row=row, col=1
        )

# ── NAV line ──────────────────────────────────────────────────────────────────
hover_nav = []
for d, v in nav_s.items():
    info = md.loc[d] if d in md.index else None
    if info is not None:
        txt = (f"<b>{d.strftime('%b %Y')}</b><br>"
               f"NAV: ₹{v:,.0f}<br>"
               f"Status: {info['status']}<br>"
               f"Nifty: {info['nifty']:,.0f} | 100MA: {info['ma100']:,.0f}<br>"
               f"<br><b>Top 5 holdings:</b><br>{info['top5']}")
    else:
        txt = f"<b>{d.strftime('%b %Y')}</b><br>NAV: ₹{v:,.0f}"
    hover_nav.append(txt)

fig.add_trace(go.Scatter(
    x=nav_s.index, y=nav_s.values,
    name="Momentum Weighted NAV",
    line=dict(color="#00d4ff", width=2),
    fill="tozeroy", fillcolor="rgba(0,212,255,0.05)",
    hovertemplate="%{customdata}<extra></extra>",
    customdata=hover_nav,
), row=1, col=1)

fig.add_trace(go.Scatter(
    x=nifty_norm.index, y=nifty_norm.values,
    name="Nifty 50 B&H",
    line=dict(color="#444444", width=1.2, dash="dot"),
    hovertemplate="<b>%{x|%b %Y}</b><br>Nifty B&H: ₹%{y:,.0f}<extra></extra>",
), row=1, col=1)

# year annotations on NAV
annual = nav_s.resample("YE").last()
for i in range(1, len(annual)):
    yr  = annual.index[i].year
    ret = (annual.iloc[i] / annual.iloc[i-1] - 1) * 100
    color = "#00ff88" if ret > 0 else "#ff4444"
    fig.add_annotation(
        x=annual.index[i], y=annual.iloc[i],
        text=f"{ret:+.0f}%",
        showarrow=False, yshift=14,
        font=dict(size=9, color=color),
        row=1, col=1
    )

# ── Drawdown ──────────────────────────────────────────────────────────────────
fig.add_trace(go.Scatter(
    x=dd_s.index, y=dd_s.values,
    name="Drawdown %",
    line=dict(color="#ff6b6b", width=1.2),
    fill="tozeroy", fillcolor="rgba(255,107,107,0.15)",
    hovertemplate="<b>%{x|%b %Y}</b><br>DD: %{y:.2f}%<extra></extra>",
), row=2, col=1)

fig.add_hline(y=-15, line_dash="dash", line_color="rgba(255,0,0,0.33)", line_width=1, row=2, col=1)
fig.add_annotation(x=nav_s.index[10], y=-15.5, text="Max DD threshold -15%",
                   font=dict(size=8, color="#ff6666"), showarrow=False, row=2, col=1)

# ── Nifty vs 100MA ────────────────────────────────────────────────────────────
fig.add_trace(go.Scatter(
    x=nifty_plot.index, y=nifty_plot.values,
    name="Nifty 50",
    line=dict(color="#888888", width=1),
    hovertemplate="<b>%{x|%b %Y}</b><br>Nifty: %{y:,.0f}<extra></extra>",
), row=3, col=1)

ma100_plot = nifty_ma100.loc[START_DATE:END_DATE]
fig.add_trace(go.Scatter(
    x=ma100_plot.index, y=ma100_plot.values,
    name="100-day MA",
    line=dict(color="#00d4ff", width=1.5),
    hovertemplate="<b>%{x|%b %Y}</b><br>100MA: %{y:,.0f}<extra></extra>",
), row=3, col=1)

# ── Layout ────────────────────────────────────────────────────────────────────
fig.update_layout(
    title=dict(
        text="Dual Momentum — Momentum Weighted | TOP_N=50 | Nifty 100MA | 6% Liquid Fund on Cash<br>"
             f"<sup>CAGR: 32.75% | Max DD: -15.0% | Final NAV: ₹38.4 Cr | Starting: ₹10L | 2006–2026</sup>",
        font=dict(size=15, color="#ffffff"),
        x=0.5
    ),
    paper_bgcolor="#0f0f0f",
    plot_bgcolor="#0f0f0f",
    font=dict(color="#aaaaaa"),
    hovermode="x unified",
    legend=dict(
        bgcolor="#1a1a1a", bordercolor="#333333", borderwidth=1,
        font=dict(color="#cccccc"), orientation="h", y=1.02, x=0
    ),
    height=850,
)

fig.update_yaxes(gridcolor="#1e1e1e", tickformat="₹,.0f", row=1, col=1)
fig.update_yaxes(gridcolor="#1e1e1e", ticksuffix="%", row=2, col=1)
fig.update_yaxes(gridcolor="#1e1e1e", tickformat=",", row=3, col=1)
fig.update_xaxes(gridcolor="#1e1e1e", rangeslider=dict(visible=False))

# range selector buttons
fig.update_xaxes(
    rangeselector=dict(
        buttons=[
            dict(count=1,  label="1Y",  step="year",  stepmode="backward"),
            dict(count=3,  label="3Y",  step="year",  stepmode="backward"),
            dict(count=5,  label="5Y",  step="year",  stepmode="backward"),
            dict(count=10, label="10Y", step="year",  stepmode="backward"),
            dict(step="all", label="All"),
        ],
        bgcolor="#1a1a1a", activecolor="#00d4ff",
        font=dict(color="#cccccc"),
    ),
    row=1, col=1
)

out_path = Path(__file__).parent / "results" / "momentum_interactive.html"
fig.write_html(str(out_path), include_plotlyjs="cdn")
print(f"Saved → {out_path}")
fig.show()
