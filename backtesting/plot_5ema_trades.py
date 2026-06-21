# ============================================================
# backtesting/plot_5ema_trades.py
#
# Plots sample trades with:
#   - Full day candlestick chart (5-min bars)
#   - 5 EMA line
#   - Signal candle highlighted
#   - Entry arrow + price label
#   - Stop loss horizontal line
#   - Exit marker + PnL label
#
# Saves individual PNGs + one combined overview figure.
# Run: python backtesting/plot_5ema_trades.py
# ============================================================

import sys
import warnings
from pathlib import Path
from datetime import timedelta

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")   # non-interactive backend (saves to file)
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
from matplotlib.patches import Rectangle

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

from backtesting.data_loader import DataLoader
from backtesting.indicators  import add_ema

# ── Config ────────────────────────────────────────────────────────────────────

RESULTS_DIR  = Path(r"G:\Trading Brain\results")
CHARTS_DIR   = RESULTS_DIR / "charts"
CHARTS_DIR.mkdir(parents=True, exist_ok=True)

N_WINNERS = 3   # top N winners to plot
N_LOSERS  = 3   # worst N losers to plot
EMA_PERIOD = 5


# ── Candlestick drawing helper ────────────────────────────────────────────────

def draw_candles(ax, df: pd.DataFrame):
    """Draw OHLCV candlesticks on ax using matplotlib date numbers."""
    GREEN = "#26a69a"
    RED   = "#ef5350"
    BAR_W = 0.0028   # candle body width in matplotlib date units (~4 min)

    for ts, row in df.iterrows():
        x    = mdates.date2num(ts.to_pydatetime())
        col  = GREEN if row["close"] >= row["open"] else RED

        # Wick (high-low)
        ax.plot([x, x], [row["low"], row["high"]],
                color=col, linewidth=0.9, zorder=1)

        # Body (open-close)
        body_lo = min(row["open"], row["close"])
        body_hi = max(row["open"], row["close"])
        body_h  = max(body_hi - body_lo, row["close"] * 0.0001)  # min visible height
        rect = Rectangle(
            (x - BAR_W / 2, body_lo), BAR_W, body_h,
            facecolor=col, edgecolor=col, linewidth=0.3, zorder=2,
        )
        ax.add_patch(rect)


# ── Single trade chart ────────────────────────────────────────────────────────

def plot_trade(trade: pd.Series, df_5min: pd.DataFrame, save_path: Path):
    """Plot one trade: full day candles + 5 EMA + trade markers."""

    # ── 1. Prepare data ───────────────────────────────────────────────────────
    df = add_ema(df_5min.copy(), period=EMA_PERIOD)
    ema_col    = f"ema_{EMA_PERIOD}"
    trade_date = pd.Timestamp(trade["entry_time"]).date()

    # Full trading day
    day_df = df[df.index.date == trade_date].copy()
    if day_df.empty:
        print(f"  No data for {trade['symbol']} on {trade_date}")
        return

    # ── 2. Key timestamps ─────────────────────────────────────────────────────
    entry_ts  = pd.Timestamp(trade["entry_time"])
    exit_ts   = pd.Timestamp(trade["exit_time"])
    signal_ts = pd.Timestamp(trade["signal_time"])

    entry_x   = mdates.date2num(entry_ts.to_pydatetime())
    exit_x    = mdates.date2num(exit_ts.to_pydatetime())
    signal_x  = mdates.date2num(signal_ts.to_pydatetime())

    entry_px  = trade["entry_price"]
    sl_px     = trade["stop_loss"]
    exit_px   = trade["exit_price"]
    pnl       = trade["pnl"]
    is_winner = pnl > 0

    # ── 3. Plot ───────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(16, 7))
    fig.patch.set_facecolor("#131722")
    ax.set_facecolor("#131722")

    # Candles
    draw_candles(ax, day_df)

    # 5 EMA
    x_ema = [mdates.date2num(t.to_pydatetime()) for t in day_df.index]
    ax.plot(x_ema, day_df[ema_col],
            color="#f9a825", linewidth=1.6, label=f"{EMA_PERIOD} EMA", zorder=3)

    # Signal candle highlight
    if signal_ts in day_df.index:
        ax.axvspan(signal_x - 0.003, signal_x + 0.003,
                   alpha=0.25, color="#ff9800", label="Signal candle", zorder=0)

    # Entry — downward red triangle
    ax.plot(entry_x, entry_px * 1.002, marker="v",
            color="#ff1744", markersize=12, zorder=5, label=f"Entry ₹{entry_px:.1f}")
    ax.annotate(f" SHORT\n ₹{entry_px:.2f}",
                xy=(entry_x, entry_px),
                color="#ff1744", fontsize=8.5, fontweight="bold",
                xytext=(entry_x, entry_px * 1.006),
                va="bottom", ha="center")

    # Stop loss — dashed red horizontal
    x_start = mdates.date2num(day_df.index[0].to_pydatetime())
    x_end   = mdates.date2num(day_df.index[-1].to_pydatetime())
    ax.hlines(sl_px, x_start, x_end,
              colors="#ff1744", linestyles="--", linewidth=1.2,
              alpha=0.7, label=f"Stop ₹{sl_px:.1f}", zorder=4)
    ax.annotate(f"SL ₹{sl_px:.2f}",
                xy=(x_end, sl_px), color="#ff1744",
                fontsize=7.5, va="center", ha="left")

    # Exit marker
    exit_color  = "#00e676" if is_winner else "#ff5252"
    exit_marker = "^" if is_winner else "v"
    ax.plot(exit_x, exit_px * (0.998 if is_winner else 1.002),
            marker=exit_marker, color=exit_color,
            markersize=12, zorder=5)
    ax.annotate(f" {trade['exit_reason'].upper()}\n ₹{exit_px:.2f}\n PnL {pnl:+,.0f}",
                xy=(exit_x, exit_px),
                color=exit_color, fontsize=8.5, fontweight="bold",
                xytext=(exit_x, exit_px * (0.993 if is_winner else 1.007)),
                va="top" if is_winner else "bottom", ha="center")

    # Vertical lines for entry/exit
    ax.axvline(entry_x, color="#ff1744", linewidth=0.6, alpha=0.4, linestyle=":")
    ax.axvline(exit_x,  color=exit_color, linewidth=0.6, alpha=0.4, linestyle=":")

    # ── 4. Axes formatting ────────────────────────────────────────────────────
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=[15, 30, 45, 0]))

    # Price padding
    price_range = day_df["high"].max() - day_df["low"].min()
    ax.set_ylim(day_df["low"].min()  - price_range * 0.05,
                day_df["high"].max() + price_range * 0.08)

    # Colour theme
    for spine in ax.spines.values():
        spine.set_edgecolor("#404040")
    ax.tick_params(colors="#cccccc", labelsize=8)
    ax.yaxis.tick_right()
    ax.grid(True, color="#2a2a3e", linewidth=0.5, alpha=0.8)

    # Title
    result_str = f"✅ WIN  +₹{pnl:,.0f}" if is_winner else f"❌ LOSS  -₹{abs(pnl):,.0f}"
    ax.set_title(
        f"{trade['symbol']}   {trade_date}   |   {result_str}   |   "
        f"Exit: {trade['exit_reason']}   |   "
        f"{int(trade['green_bars'])} green bars above {EMA_PERIOD} EMA",
        color="white", fontsize=11, fontweight="bold", pad=10,
    )

    # Legend
    leg = ax.legend(loc="upper left", fontsize=8,
                    facecolor="#1e1e2e", edgecolor="#404040",
                    labelcolor="white", framealpha=0.8)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close()
    print(f"  Saved → {save_path.name}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # ── Load trades CSV ───────────────────────────────────────────────────────
    trade_files = sorted(RESULTS_DIR.glob("5ema_short_trades_*.csv"))
    if not trade_files:
        print("No trade CSV found. Run run_backtest_5ema.py first.")
        return

    trades_file = trade_files[-1]
    print(f"\n  Reading trades from: {trades_file.name}")
    df_trades = pd.read_csv(trades_file)
    df_trades["entry_time"]  = pd.to_datetime(df_trades["entry_time"])
    df_trades["exit_time"]   = pd.to_datetime(df_trades["exit_time"])
    df_trades["signal_time"] = pd.to_datetime(df_trades["signal_time"])
    print(f"  Total trades: {len(df_trades):,}")

    # ── Pick sample trades ────────────────────────────────────────────────────
    winners = df_trades[df_trades["pnl"] > 500].nlargest(N_WINNERS, "pnl")
    losers  = df_trades[df_trades["pnl"] < -500].nsmallest(N_LOSERS, "pnl")
    sample  = pd.concat([winners, losers]).reset_index(drop=True)

    print(f"\n  Plotting {len(winners)} winners + {len(losers)} losers:\n")
    for _, t in sample.iterrows():
        tag = "WIN" if t["pnl"] > 0 else "LOSS"
        print(f"   {tag:4s}  {t['symbol']:<28}  {str(t['entry_time'].date())}  "
              f"PnL ₹{t['pnl']:+,.0f}")

    # ── Load data & plot each trade ───────────────────────────────────────────
    loader = DataLoader()
    cache  = {}

    print()
    for i, (_, trade) in enumerate(sample.iterrows(), 1):
        symbol = trade["symbol"]
        label  = "winner" if trade["pnl"] > 0 else "loser"
        date   = pd.Timestamp(trade["entry_time"]).date()

        print(f"  [{i}/{len(sample)}] {symbol} {date} ({label})")

        if symbol not in cache:
            try:
                cache[symbol] = loader.load(symbol)
            except Exception as e:
                print(f"    SKIP — could not load data: {e}")
                continue

        safe_sym = symbol.replace(":", "_").replace("-", "_")
        out_path = CHARTS_DIR / f"{i:02d}_{label}_{safe_sym}_{date}.png"

        try:
            plot_trade(trade, cache[symbol], out_path)
        except Exception as e:
            print(f"    ERROR: {e}")

    # ── Combined overview figure ──────────────────────────────────────────────
    chart_files = sorted(CHARTS_DIR.glob("*.png"))
    if len(chart_files) >= 2:
        n    = len(chart_files)
        ncol = 2
        nrow = (n + 1) // ncol

        fig, axes = plt.subplots(nrow, ncol, figsize=(20, 7 * nrow))
        fig.patch.set_facecolor("#0d0d1a")
        axes = axes.flatten()

        for ax, img_path in zip(axes, chart_files):
            img = plt.imread(str(img_path))
            ax.imshow(img)
            ax.axis("off")

        for ax in axes[len(chart_files):]:
            ax.set_visible(False)

        fig.suptitle("5 EMA Short — Sample Trade Charts", color="white",
                     fontsize=14, fontweight="bold", y=1.01)
        plt.tight_layout()
        overview_path = CHARTS_DIR / "00_overview.png"
        plt.savefig(overview_path, dpi=120, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close()
        print(f"\n  Combined overview → {overview_path}")

    print(f"\n  All charts saved to: {CHARTS_DIR}\n")


if __name__ == "__main__":
    main()
