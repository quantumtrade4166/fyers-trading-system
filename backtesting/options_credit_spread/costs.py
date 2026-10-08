"""Full transaction-cost model for NIFTY index-option credit spreads.

The backtest P&L is gross. This applies the charges a real account pays.

Per round-trip trade there are FOUR legs:
    entry: SELL short strike, BUY long strike
    exit : BUY  short strike, SELL long strike

Charges (Indian index options, discount broker):
  brokerage      Rs 20 per order, flat            -> Rs 80 per round trip
  STT            on the SELL side only, % of premium turnover.
                 0.0625% before 2024-10-01, 0.10% from 2024-10-01.
  exchange txn   NSE, % of premium turnover, both sides.
                 0.0530% before 2024-10-01, 0.0350% from 2024-10-01.
  SEBI           0.0001% of premium turnover (Rs 10 per crore)
  stamp duty     0.003% on the BUY side only
  GST            18% on (brokerage + exchange txn + SEBI)
  slippage       modelled separately as a % of premium per leg, since it is an
                 execution cost, not a statutory one.

The strategy squares off at 15:15 on expiry day, so positions are never
exercised — the much larger 0.125% exercise STT does not apply.

Rates are assumptions, not gospel: verify against your own contract notes
before sizing anything on these numbers.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

BROKERAGE_PER_ORDER = 20.0
SEBI_RATE = 0.000001          # 0.0001%
STAMP_RATE = 0.00003          # 0.003%, buy side only
GST_RATE = 0.18
RATE_CHANGE = pd.Timestamp("2024-10-01")


def stt_rate(ts):
    return 0.0010 if pd.Timestamp(ts) >= RATE_CHANGE else 0.000625


def exch_rate(ts):
    return 0.000350 if pd.Timestamp(ts) >= RATE_CHANGE else 0.000530


def trade_costs(row, qty, slippage_pct):
    """Total rupee cost for one round-trip spread."""
    entry_ts, exit_ts = row["entry_time"], row["exit_time"]

    se, le = row["short_entry_price"], row["long_entry_price"]
    sx, lx = row.get("short_exit_price"), row.get("long_exit_price")
    # Legs settled by approximation have no exit print; fall back to entry
    # premium so the cost is estimated rather than silently zero.
    sx = se if pd.isna(sx) else sx
    lx = le if pd.isna(lx) else lx

    sells = [(se, entry_ts), (lx, exit_ts)]      # short opened, long closed
    buys = [(le, entry_ts), (sx, exit_ts)]       # long opened, short closed

    sell_val = sum(p * qty for p, _ in sells)
    buy_val = sum(p * qty for p, _ in buys)
    turnover = sell_val + buy_val

    stt = sum(p * qty * stt_rate(t) for p, t in sells)
    exch = sum(p * qty * exch_rate(t) for p, t in sells + buys)
    sebi = turnover * SEBI_RATE
    stamp = buy_val * STAMP_RATE
    brokerage = BROKERAGE_PER_ORDER * 4
    gst = (brokerage + exch + sebi) * GST_RATE
    slippage = turnover * slippage_pct

    return brokerage + stt + exch + sebi + stamp + gst + slippage


def apply(df, lots=10, lot_size=75, slippage_pct=0.0):
    qty = lots * lot_size
    df = df.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    df["gross"] = df["pnl_rupees"] * lots
    df["cost"] = df.apply(lambda r: trade_costs(r, qty, slippage_pct), axis=1)
    df["net"] = df["gross"] - df["cost"]
    return df.sort_values("exit_time").reset_index(drop=True)


def metrics(df, capital=600_000):
    eq = capital + df["net"].cumsum()
    dd = (eq / eq.cummax() - 1).min() * 100
    yrs = (df["exit_time"].iloc[-1] - df["exit_time"].iloc[0]).days / 365.25
    fin = eq.iloc[-1]
    cagr = ((fin / capital) ** (1 / yrs) - 1) * 100 if fin > 0 else float("nan")
    gw = df.loc[df.net > 0, "net"].sum()
    gl = -df.loc[df.net < 0, "net"].sum()
    return {
        "trades": len(df), "win": (df.net > 0).mean() * 100,
        "pf": gw / gl if gl else float("inf"),
        "gross": df["gross"].sum(), "costs": df["cost"].sum(),
        "net": df["net"].sum(), "final": fin,
        "total_ret": (fin / capital - 1) * 100, "cagr": cagr, "dd": dd,
    }
