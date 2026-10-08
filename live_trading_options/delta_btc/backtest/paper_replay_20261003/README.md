# Paper-day replay kit — 2026-10-03

Everything used on 2026-10-03/04 to test rule changes on the 16 live PAPER days of the
BTC VWAP Strangle ist_day version (16-Sep → 02-Oct 2026). Vault: [[BTC VWAP Split-Leg Strangle_Results]].

| File | What |
|---|---|
| `ist/*.json` | the paper engine's own day files (candles, VWAP, trades, per-minute MTM) pulled from `/api/btcvwap/day?version=ist_day` |
| `legs.parquet` | Delta 1-min MARK candles for the 32 legs those days traded (`fetch_legs.py`) |
| `replay_split.py` | replays the PAPER trades with split-leg exits: `both` / `C` (60% trail) / `D` (own VWAP), max 3/4, ±$70 stop; `python replay_split.py 1.5` adds the 1.5x VWAP cap |
| `compare_paper.py` + `trade_compare.csv` | paper days vs a fresh backtest on the same days (needs `Bitcoin options data/raw/2026-09-16..10-02`) |
| `dl_recent.py`, `probe_fill.py` | download those days from Delta (newest-first product list) and fill contracts Delta's list skipped |
| `liq_snap.py` | one-off Binance vs Delta order-book snapshot |

Run from this folder with the project venv.
