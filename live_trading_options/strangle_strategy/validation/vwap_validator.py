"""
Phase 0 — Combined Premium VWAP Validation
==========================================

Purpose: prove that our combined-premium VWAP calculation matches the trader's
iCharts chart BEFORE any strategy / live-trading code is written.

What it does:
  1. Pull 5-min OHLCV for a CE leg and a PE leg from Fyers history API.
  2. Build the combined premium series (CE + PE for O/H/L/C, and CE+PE volume).
  3. Compute intraday cumulative VWAP from 9:15 AM, reset each day.
  4. Render an interactive Plotly chart:
        - combined premium candlesticks (5-min)
        - VWAP line overlay
        - entry markers  (red ▼) : candle closes BELOW vwap AND is red (close<open)
        - exit markers   (green ▲): candle closes ABOVE vwap
        - day-selector dropdown
        - full hover with OHLCV + VWAP
  5. Save to an .html file and open it for side-by-side comparison with iCharts.

VWAP formula (exactly as specified):
    VWAP = Σ(combined_close × combined_volume) / Σ(combined_volume)
    cumulative from 9:15 AM, resets every day.

Usage:
    python live_trading_options/strangle_strategy/validation/vwap_validator.py \
        --ce NSE:NIFTY26JUN24250CE \
        --pe NSE:NIFTY26JUN23450PE \
        --from 2026-06-19 --to 2026-06-24
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import argparse
import webbrowser
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from fyers_apiv3 import fyersModel

# ── Project paths / credentials ──────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[3]          # G:\fyers_data_pipeline
sys.path.append(str(ROOT))

CLIENT_ID  = "W09OMXQB8J-100"
TOKEN_FILE = ROOT / "config" / "access_token.txt"
OUT_DIR    = Path(__file__).resolve().parent / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

RESOLUTION = "1"          # fetch 1-min legs; combined candle rolled up to 5-min
MKT_START  = pd.Timestamp("09:15").time()
MKT_END    = pd.Timestamp("15:30").time()


# ── Token / client ───────────────────────────────────────────────────────
def load_raw_token() -> str:
    """Read the token string directly (ignore the date check in fyers_auth —
    we must NEVER trigger an interactive/browser login from the local machine,
    that would invalidate the VPS token and kill the live feed)."""
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(f"No token file at {TOKEN_FILE}")
    payload = json.loads(TOKEN_FILE.read_text())
    print(f"  token date in file: {payload.get('date')}")
    return payload["token"]


def get_client() -> fyersModel.FyersModel:
    token = load_raw_token()
    return fyersModel.FyersModel(
        client_id=CLIENT_ID, token=token, log_path=str(ROOT / "logs"), is_async=False
    )


# ── Data fetch ───────────────────────────────────────────────────────────
def fetch_legs(client, symbol: str, from_date: str, to_date: str) -> pd.DataFrame:
    """Fetch 1-MINUTE OHLCV for one leg. We use 1-min (not 5-min) so the
    combined candle can be reconstructed with smoothed wicks (see build_combined).
    Spanning >~100 days would exceed the Fyers intraday limit — validation
    windows are a few days, so a single call is fine."""
    data = {
        "symbol":      symbol,
        "resolution":  RESOLUTION,        # "1" — one-minute bars
        "date_format": "1",
        "range_from":  from_date,
        "range_to":    to_date,
        "cont_flag":   "1",
    }
    resp = client.history(data=data)
    if resp.get("s") != "ok":
        raise RuntimeError(f"Fyers history failed for {symbol}: {resp}")

    candles = resp.get("candles", [])
    if not candles:
        raise RuntimeError(f"No candles returned for {symbol} ({from_date}→{to_date})")

    cols = ["epoch", "open", "high", "low", "close", "volume"]
    if len(candles[0]) == 7:
        cols.append("oi")
    df = pd.DataFrame(candles, columns=cols)
    df["datetime"] = (
        pd.to_datetime(df["epoch"], unit="s")
        .dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    )
    df = df.drop(columns=["epoch"])
    df = df[(df["datetime"].dt.time >= MKT_START) & (df["datetime"].dt.time <= MKT_END)]
    # Fyers occasionally returns the most-recent day twice — dedupe on timestamp.
    df = df.drop_duplicates(subset="datetime", keep="first")
    return df[["datetime", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


# ── Combined premium + VWAP ──────────────────────────────────────────────
# VWAP method CONFIRMED against iCharts (2026-06-25, Sensex expiry-day):
#   - combined candle built from 1-MIN legs; high/low taken from SYNCHRONIZED
#     open & close points (CE+PE sampled at the same instant), NOT from summing
#     each leg's independent 5-min high/low (that inflates the wicks).
#   - VWAP = Σ(typical × vol) / Σ(vol), typical = (high+low+close)/3, resets daily.
# Live strategy will build the same candle from tick data → even tighter wicks.
def build_combined(ce: pd.DataFrame, pe: pd.DataFrame) -> pd.DataFrame:
    m = ce.merge(pe, on="datetime", suffixes=("_ce", "_pe"))
    m["c_open"]  = m["open_ce"]  + m["open_pe"]
    m["c_close"] = m["close_ce"] + m["close_pe"]
    m["c_vol"]   = m["volume_ce"] + m["volume_pe"]
    m = m.set_index("datetime")

    def agg(x: pd.DataFrame) -> pd.Series:
        pts = pd.concat([x["c_open"], x["c_close"]])      # synchronized price points
        return pd.Series({
            "open":   x["c_open"].iloc[0],
            "high":   pts.max(),
            "low":    pts.min(),
            "close":  x["c_close"].iloc[-1],
            "volume": x["c_vol"].sum(),
        })

    bars = (
        m.resample("5min", label="left", closed="left", origin="start_day")
         .apply(agg).dropna()
    )
    bars = bars[(bars.index.time >= MKT_START) & (bars.index.time <= MKT_END)]
    out = bars.reset_index()
    out["date"] = out["datetime"].dt.date

    # cumulative typical-price VWAP per day
    out["typ"] = (out["high"] + out["low"] + out["close"]) / 3
    out["pv"]  = out["typ"] * out["volume"]
    grp = out.groupby("date")
    out["vwap"] = grp["pv"].cumsum() / grp["volume"].cumsum()
    out = out.drop(columns=["pv"])

    # signals (evaluated at candle close)
    out["is_red"]       = out["close"] < out["open"]
    out["below_vwap"]   = out["close"] < out["vwap"]
    out["above_vwap"]   = out["close"] > out["vwap"]
    out["entry_signal"] = out["below_vwap"] & out["is_red"]
    out["exit_signal"]  = out["above_vwap"]
    return out


# ── Chart ────────────────────────────────────────────────────────────────
def render_chart(df: pd.DataFrame, ce_sym: str, pe_sym: str) -> Path:
    dates = sorted(df["date"].unique())
    fig = go.Figure()
    traces_per_day = 4

    for d in dates:
        day = df[df["date"] == d]
        x = day["datetime"]

        fig.add_trace(go.Candlestick(
            x=x, open=day["open"], high=day["high"], low=day["low"], close=day["close"],
            name="Combined Premium", increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350", visible=False,
            customdata=day["volume"],
            hovertext=[f"Vol {int(v):,}" for v in day["volume"]],
        ))
        fig.add_trace(go.Scatter(
            x=x, y=day["vwap"], mode="lines", name="VWAP",
            line=dict(color="#fb8c00", width=2), visible=False,
            hovertemplate="VWAP %{y:.2f}<extra></extra>",
        ))
        ent = day[day["entry_signal"]]
        fig.add_trace(go.Scatter(
            x=ent["datetime"], y=ent["low"] - 1, mode="markers", name="Entry (close<VWAP & red)",
            marker=dict(symbol="triangle-down", size=12, color="#d50000"),
            visible=False, hovertemplate="ENTRY @ %{x|%H:%M}<extra></extra>",
        ))
        ex = day[day["exit_signal"]]
        fig.add_trace(go.Scatter(
            x=ex["datetime"], y=ex["high"] + 1, mode="markers", name="Exit (close>VWAP)",
            marker=dict(symbol="triangle-up", size=12, color="#00c853"),
            visible=False, hovertemplate="EXIT @ %{x|%H:%M}<extra></extra>",
        ))

    # default: show last day (the day under validation)
    default_idx = len(dates) - 1
    for t in range(traces_per_day):
        fig.data[default_idx * traces_per_day + t].visible = True

    # dropdown
    buttons = []
    for i, d in enumerate(dates):
        vis = [False] * (len(dates) * traces_per_day)
        for t in range(traces_per_day):
            vis[i * traces_per_day + t] = True
        buttons.append(dict(
            label=str(d), method="update",
            args=[{"visible": vis},
                  {"title": f"Combined Premium VWAP — {ce_sym} + {pe_sym}  |  {d}"}],
        ))

    fig.update_layout(
        title=f"Combined Premium VWAP — {ce_sym} + {pe_sym}  |  {dates[default_idx]}",
        updatemenus=[dict(buttons=buttons, direction="down", x=0.0, y=1.15,
                          xanchor="left", showactive=True)],
        xaxis=dict(title="Time (IST)", rangeslider=dict(visible=False),
                   type="date", tickformat="%H:%M"),
        yaxis=dict(title="Combined Premium (CE+PE)"),
        template="plotly_white", height=750, hovermode="x unified",
    )

    out = OUT_DIR / "vwap_validation.html"
    fig.write_html(str(out))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ce", default="NSE:NIFTY26JUN24250CE")
    ap.add_argument("--pe", default="NSE:NIFTY26JUN23450PE")
    ap.add_argument("--from", dest="from_date", default="2026-06-19")
    ap.add_argument("--to",   dest="to_date",   default="2026-06-24")
    args = ap.parse_args()

    print("Building Fyers client...")
    client = get_client()

    print(f"Fetching CE {args.ce} (1-min) ...")
    ce = fetch_legs(client, args.ce, args.from_date, args.to_date)
    print(f"  {len(ce)} 1-min bars  ({ce['datetime'].min()} → {ce['datetime'].max()})")

    print(f"Fetching PE {args.pe} (1-min) ...")
    pe = fetch_legs(client, args.pe, args.from_date, args.to_date)
    print(f"  {len(pe)} 1-min bars  ({pe['datetime'].min()} → {pe['datetime'].max()})")

    combined = build_combined(ce, pe)
    print(f"Combined: {len(combined)} aligned bars across {combined['date'].nunique()} day(s)")

    # quick text peek for the validation day (last day in range)
    last_day = sorted(combined["date"].unique())[-1]
    day = combined[combined["date"] == last_day]
    print(f"\n── {last_day}: first / last 3 combined bars ──")
    show = day[["datetime", "open", "high", "low", "close", "volume", "vwap"]]
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(show.head(3).to_string(index=False))
        print("...")
        print(show.tail(3).to_string(index=False))
    print(f"\nEntry signals: {int(day['entry_signal'].sum())}   "
          f"Exit signals: {int(day['exit_signal'].sum())}")

    out = render_chart(combined, args.ce, args.pe)
    print(f"\nChart saved: {out}")
    webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
