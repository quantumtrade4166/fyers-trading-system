"""P&L, costs, metrics and charts. All money metrics are NET unless labelled gross.

Costs reuse backtesting/options_pivot_intraday/costs.py (single-leg naked short:
brokerage Rs 20/order sliced at freeze qty, STT sell side, NSE txn both sides,
SEBI, stamp buy side, GST) -- dated rate schedules, verify against a contract note.
Slippage is a separate cost: cfg.slippage_points per order x qty.

Sharpe/Sortino: daily NET P&L / capital over EVERY trading day in the range
(no-trade days = 0), mean/std x sqrt(252), risk-free 0. With ~40 days these are
indicative only. No CAGR is reported for a period under one year.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from backtesting.options_pivot_intraday.costs import statutory_costs
from backtesting.options_st_directional.config import lot_size


def add_pnl(trades: pd.DataFrame, cfg) -> pd.DataFrame:
    df = trades.copy()
    if df.empty:
        return df
    df["lot_size"] = df["expiry"].map(lot_size)          # by contract expiry, not trade date
    df["lots"] = cfg.lots
    df["qty"] = df["lot_size"] * cfg.lots
    df["points"] = df["entry_price"] - df["exit_price"]
    df["gross_pnl"] = df["points"] * df["qty"]
    rows = [statutory_costs(r.entry_price, r.exit_price, r.qty, r.date)
            for r in df.itertuples()]
    for k in ("brokerage", "stt", "exch", "sebi", "stamp", "gst"):
        df[f"cost_{k}"] = [c[k] for c in rows]
    df["cost_slippage"] = cfg.slippage_points * 2 * df["qty"]
    df["cost_total"] = [c["total"] for c in rows] + df["cost_slippage"]
    df["net_pnl"] = df["gross_pnl"] - df["cost_total"]
    df["entry_ts"] = pd.to_datetime(df["entry_ts"])
    df["exit_ts"] = pd.to_datetime(df["exit_ts"])
    df["hold_min"] = (df["exit_ts"] - df["entry_ts"]).dt.total_seconds() / 60
    df["first_exit_check_exit"] = (
        df["exit_reason"].eq("option_st")
        & (df["exit_signal_ts"] - df["entry_ts"] <= pd.Timedelta(minutes=5)))
    return df


def _streaks(wins: pd.Series):
    best = {True: 0, False: 0}
    cur, prev = 0, None
    for w in wins:
        cur = cur + 1 if w == prev else 1
        prev = w
        best[w] = max(best[w], cur)
    return best[True], best[False]


def metrics(df: pd.DataFrame, days: list, cfg) -> tuple[dict, pd.Series]:
    daily = pd.Series(0.0, index=pd.to_datetime(days))
    if not df.empty:
        daily = daily.add(df.groupby(pd.to_datetime(df["date"]))["net_pnl"].sum(),
                          fill_value=0.0)
    eq = cfg.capital + daily.cumsum()
    peak = eq.cummax()
    dd = eq - peak
    # drawdown duration in trading days (peak to recovery, or to the end)
    dur, longest = 0, 0
    for v in dd:
        dur = dur + 1 if v < 0 else 0
        longest = max(longest, dur)

    ret = daily / cfg.capital
    sd = ret.std(ddof=1)
    down = ret[ret < 0]
    m = {"period": f"{days[0]} -> {days[-1]}", "trading_days": len(days),
         "days_with_trades": int(df["date"].nunique()) if not df.empty else 0}
    if df.empty:
        m["trades"] = 0
        return m, daily

    wins, losses = df[df.net_pnl > 0], df[df.net_pnl <= 0]
    gw, gl = wins.net_pnl.sum(), -losses.net_pnl.sum()
    ws, ls = _streaks(df.sort_values("entry_ts")["net_pnl"] > 0)
    sess_min = 360 * len(days)                      # 09:15-15:15
    m.update({
        "trades": len(df), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": 100 * len(wins) / len(df),
        "gross_pnl": df.gross_pnl.sum(), "costs": df.cost_total.sum(),
        "net_pnl": df.net_pnl.sum(),
        "avg_win": wins.net_pnl.mean() if len(wins) else 0.0,
        "avg_loss": losses.net_pnl.mean() if len(losses) else 0.0,
        "expectancy": df.net_pnl.mean(),
        "profit_factor": gw / gl if gl else float("inf"),
        "max_dd_rs": dd.min(), "max_dd_pct": 100 * (dd / peak).min(),
        "max_dd_duration_days": longest,
        "return_on_capital_pct": 100 * daily.sum() / cfg.capital,
        "sharpe_ann": (ret.mean() / sd * np.sqrt(252)) if sd > 0 else float("nan"),
        "sortino_ann": (ret.mean() / down.std(ddof=1) * np.sqrt(252))
                       if len(down) > 1 else float("nan"),
        "avg_hold_min": df.hold_min.mean(),
        "longest_win_streak": ws, "longest_loss_streak": ls,
        "time_in_market_pct": 100 * df.hold_min.sum() / sess_min,
        "margin_utilisation_when_in_pct": 100 * cfg.margin_per_lot * cfg.lots / cfg.capital,
        "pending_signal_trades": int(df.pending_while_open.sum()),
        "exits_at_first_check": int(df.first_exit_check_exit.sum()),
        "square_off_exits": int((df.exit_reason == "square_off_1515").sum()),
        "best_day": daily.max(), "worst_day": daily.min(),
    })
    return m, daily


def charts(df: pd.DataFrame, daily: pd.Series, cfg, out_dir):
    eq = cfg.capital + daily.cumsum()
    dd = eq - eq.cummax()
    fig, ax = plt.subplots(3, 2, figsize=(15, 12))
    ax[0, 0].plot(eq.index, eq.values, color="#1f6feb"); ax[0, 0].set_title("Equity (net, Rs)")
    ax[0, 1].fill_between(dd.index, dd.values, 0, color="#d1242f", alpha=.5)
    ax[0, 1].set_title("Drawdown (Rs)")
    ax[1, 0].bar(daily.index, daily.values,
                 color=np.where(daily.values >= 0, "#1a7f37", "#d1242f"))
    ax[1, 0].set_title("Daily net P&L")
    monthly = daily.groupby(daily.index.to_period("M")).sum()
    ax[1, 1].bar(monthly.index.astype(str), monthly.values,
                 color=np.where(monthly.values >= 0, "#1a7f37", "#d1242f"))
    ax[1, 1].set_title("Monthly net P&L (yearly = sum; < 1 year shown)")
    if not df.empty:
        ax[2, 0].hist(df.net_pnl, bins=30, color="#8250df")
        ax[2, 0].axvline(0, color="k", lw=.8)
        ax[2, 0].set_title("Trade net P&L distribution")
        w, l = df[df.net_pnl > 0], df[df.net_pnl <= 0]
        ax[2, 1].bar(["wins", "losses"], [len(w), len(l)], color=["#1a7f37", "#d1242f"])
        ax[2, 1].set_title(f"Wins vs losses | avg win {w.net_pnl.mean():,.0f} "
                           f"| avg loss {l.net_pnl.mean():,.0f}")
    for a in ax.flat:
        a.grid(alpha=.3)
        a.tick_params(axis="x", rotation=30)
    fig.suptitle(f"NIFTY ST directional option selling | {cfg.tag} | "
                 f"{cfg.start} -> {cfg.end} | {cfg.lots} lot | net of costs")
    fig.tight_layout()
    fig.savefig(out_dir / "summary.png", dpi=110)
    plt.close(fig)


def day_chart(day, provider, result, out_dir):
    """Spot + spot ST on top; each traded contract's premium + its ST below, with
    the reference low path and entry/exit markers."""
    spot = provider.spot_candles(day)
    trades = result["trades"]
    fig, ax = plt.subplots(1 + max(1, len(trades)), 1,
                           figsize=(14, 3.2 * (1 + max(1, len(trades)))), sharex=True)
    ax = np.atleast_1d(ax)
    x = spot.index
    ax[0].plot(x, spot.close, color="k", lw=1, label="NIFTY 5m close")
    for dval, col in ((1, "#1a7f37"), (-1, "#d1242f")):
        ax[0].scatter(x[spot.st_dir == dval], spot.st[spot.st_dir == dval], s=6, color=col)
    for r in result["reversals"]:
        ax[0].axvline(r["ts"], color="#9a6700", lw=.8, ls="--")
    ax[0].set_title(f"{day}  NIFTY + ST(10,3)  (dashed = reversal confirmed)")
    setups = {s.setup_id: s for s in result["setups"]}
    for i, tr in enumerate(trades, start=1):
        s = setups[tr["setup_id"]]
        oc = provider.option_candles(day, s.contract)
        ax[i].plot(oc.index, oc.close, color="k", lw=1)
        ax[i].plot(oc.index, oc.st, color="#d1242f", lw=1, label="option ST")
        if s.ref_candle_ts is not None:
            path = [(s.ref_candle_ts, s.ref_low_initial)] + [(a, n) for a, _, n in s.ref_updates]
            path.append((s.signal_candle_ts or path[-1][0], s.signal_ref or path[-1][1]))
            ax[i].step([p[0] for p in path], [p[1] for p in path], where="post",
                       color="#1f6feb", lw=1.2, label="reference low")
        ax[i].scatter([tr["entry_ts"]], [tr["entry_price"]], marker="v", s=80, color="#d1242f",
                      zorder=5, label="sell")
        ax[i].scatter([tr["exit_ts"]], [tr["exit_price"]], marker="^", s=80, color="#1a7f37",
                      zorder=5, label="buy back")
        ax[i].set_title(f"{tr['trade_id']}  {tr['strike']}{tr['opt_type']} exp {tr['expiry']}  "
                        f"{tr['entry_price']} -> {tr['exit_price']}  ({tr['exit_reason']})")
        ax[i].legend(loc="upper right", fontsize=7)
    for a in ax:
        a.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(out_dir / f"{day}.png", dpi=90)
    plt.close(fig)
