# BTC Futures Arbitrage Monitor — Delta Exchange India vs Binance

**READ-ONLY.** Public market data only. There is no API key anywhere in this
package and no code that can place, cancel or modify an order.

The live view is the **₿ BTC Arbitrage** tab on the dashboard
(https://dash.trading.contact). This folder is the background process that feeds it.

```
crypto/btc_arbitrage/
├── main.py                    the monitor process (asyncio)
├── config.py                  EVERY tunable: symbols, fees, thresholds, endpoints
├── exchanges/  base.py        shared reconnect loop
│               delta.py       Delta India: l1_orderbook + funding_rate sockets
│               binance.py     Binance futures: <symbol>@bookTicker socket, REST funding
├── core/       market_data.py latest valid quote per exchange + connection state
│               spread_engine.py executable spreads, signs, data-quality check
│               fees.py        taker fees (+ GST on Delta), entry / round trip
│               opportunity_detector.py STARTED / CHANGED / ENDED events
│               depth.py       order-book walk for a size (ready for v2)
├── storage/    database.py    1-second history, one SQLite file per IST day
├── tests/      test_spread.py 75 checks, no network
├── tools/      install_task.ps1  VPS scheduled task "BtcArbMonitor"
├── run_monitor.bat
├── data/  spread_db/YYYY-MM-DD.sqlite   state/TICK.json   state/events.jsonl
└── logs/  application.log   opportunities.csv
deployment/btc_arb_api.py      dashboard API (reads the files above, never writes)
```

## 1. Installation
Nothing new to install: `websockets`, `requests` and `fastapi` are already in the
project `.venv` (see `requirements.txt`).

## 2. Configuration
Everything is in `config.py`. Edit, then restart the monitor. The dashboard reads
the values back from the monitor, so the web page never needs editing.

## 3. Start / stop
```
.venv\Scripts\python.exe crypto\btc_arbitrage\main.py          # foreground
crypto\btc_arbitrage\run_monitor.bat                             # same, via launcher
powershell -ExecutionPolicy Bypass -File crypto\btc_arbitrage\tools\install_task.ps1          # VPS: install + start the task
powershell -ExecutionPolicy Bypass -File crypto\btc_arbitrage\tools\install_task.ps1 -Status  # is it running?
```
A second copy exits at once (lock port **47664**). To stop it on the VPS, kill the
PID that `-Status` shows. Never use `taskkill /IM python.exe`, which kills every engine.

Tests: `.venv\Scripts\python.exe crypto\btc_arbitrage\tests\test_spread.py`

## Contracts (verified live 2026-10-03)
| Exchange | Symbol | Type | Size | Quote |
|---|---|---|---|---|
| Delta India | `BTCUSD` | perpetual | 0.001 BTC / contract | USD |
| Binance USD-M | `BTCUSDT` (default) | perpetual | qty in BTC | **USDT** |
| Binance USD-M | `BTCUSDC` | perpetual | qty in BTC | USDC |
| Binance COIN-M | `BTCUSD_PERP` | perpetual, **inverse** | $100 / contract | USD |

Delta India lists **no dated BTC futures**, so the only like-for-like pair is
perpetual vs perpetual. Binance's quarterlies are refused at start-up. On every
start the monitor checks both contracts against the exchanges (type, underlying,
contract size, quote). If anything differs it **refuses to run** and shows why on
the tab.

## 4. How the spread is calculated (USD per 1 BTC)
```
s1 = Binance_bid − Delta_ask      BUY Delta,   SELL Binance
s2 = Delta_bid  − Binance_ask     BUY Binance, SELL Delta
mid_spread = Binance_mid − Delta_mid
```
At most one of s1 / s2 can be positive. In a normal market both are negative.

**Correction to the original prompt.** It proposed `net = s1 if s1 >= 0 else −s2`.
In a normal market s2 is negative, so −s2 is positive. That line would sit above
zero almost all the time and look like a constant opportunity. `tests/test_spread.py`
proves it. Instead:

* **the line** is `mid_spread`. It moves continuously around zero.
* **the ribbon** is green = `s1` (Binance bid − Delta ask) and red = `−s2`
  (Binance ask − Delta bid). The ribbon is the mid ± both half-spreads.
* **executable** only when the whole ribbon is off zero: green above 0 means buy
  Delta / sell Binance; red below 0 means buy Binance / sell Delta.
* **worth taking** only when it clears the dashed hurdle (fees + threshold).

Quotes arrive at different moments. The monitor keeps the latest valid quote from
each exchange and recomputes whenever a price changes. Every row stores both
quotes' ages.

## 5. Fees
```
Delta leg   = price × DELTA_TAKER_FEE × (1 + DELTA_FEE_GST)     0.05% + 18% GST = 0.059%
Binance leg = price × BINANCE_TAKER_FEE                         0.05%
entry       = Delta leg + Binance leg                           ≈ 0.109%
round_trip  = 2 × entry  (open both + close both, 4 fills)      ≈ 0.218% ≈ $183 / BTC at $84k
```
`FEE_MODE = "round_trip"` is the default, because a spread trade has to be
closed too. At start-up the monitor logs a warning if Delta's live fee differs
from the config. The config value is still the one used.

## 6. Change the contract
In `config.py` set `BINANCE_SYMBOL` to `BTCUSDC` or `BTCUSD_PERP` (or add a new
entry to `BINANCE_CONTRACTS` with its market / quote / qty unit). Then restart.
`ADJUST_QUOTE_TO_USD = True` converts Binance USDT prices to USD using the live
USDC/USDT rate. 1 basis point of USDT premium is about $8, which is the same size
as the spread itself.

## 7. Change the threshold
`OPPORTUNITY_THRESHOLD_USD` and `OPPORTUNITY_THRESHOLD_PCT`: the net spread after
fees must clear **both**. `OPPORTUNITY_MIN_MS` is how long it must hold.
`OPPORTUNITY_CHANGE_USD` is how far a running opportunity must move to be logged
again.

## 8. Reading the chart
* **Above zero (blue)**: Binance is pricing BTC higher than Delta.
* **Below zero (amber)**: Delta is pricing BTC higher than Binance.
* **Near zero**: the two markets are aligned.

The status card says **OPPORTUNITY** only when data is fresh on both sides
(`MAX_DATA_AGE_MS`), the quotes are close in time (`MAX_QUOTE_TIME_DIFF_MS`), and
the net spread clears the threshold for `OPPORTUNITY_MIN_MS`. Otherwise it shows
STALE DATA, DESYNC, DISCONNECTED or NO OPPORTUNITY.

Latency = exchange timestamp → our receipt, corrected by the measured clock offset
(against Binance server time every 5 min). Ages use our own monotonic clock, so a
wrong exchange clock cannot make old data look fresh.

## 9. Troubleshooting
* **Tab says MONITOR DOWN / STALE**: run `install_task.ps1 -Status`, then read
  `logs/application.log`.
* **DELTA / BINANCE DISCONNECTED**: the client reconnects by itself after 1, 2, 4 …
  up to 60 s. The reason is in the feed card and the log. A socket that goes
  silent for 30 s is rebuilt. Binance also closes every socket after 24 h, which
  is normal and reconnects at once.
* **"REFUSING TO START"**: the configured contract no longer matches the
  exchange (delisted, renamed, wrong market). Fix `config.py`.
* **Clock offset is large**: harmless for the spread. It only skews the
  latency figure.

## Storage
One row per second: the last quotes, plus the best of each direction inside
that second, so blips survive downsampling. About 12 MB a day, kept
`RETENTION_DAYS` (90). Columns are listed in `storage/database.py`. Opportunity
events go to `logs/opportunities.csv`.

Links: [[BTC Arbitrage Monitor]] · [[Trading System]]
