# BTC Delta-Neutral Strangle — Delta Exchange India

A port of the NSE [[Delta Neutral Strangle]] (`live_trading_options/delta_neutral/`,
NIFTY + SENSEX on Zerodha) to Bitcoin options on Delta Exchange India.

**The strategy is unchanged. The session is the experiment.**

BTC trades 24/7, so "a trading day" is a choice rather than something the exchange
hands you. Three profiles run **side by side on one chain feed** — identical
snapshot, identical instant — so any difference in their results after a month is
attributable to the session rule and nothing else.

| | Profile | Clock (IST) | Exposure | Temperament |
|---|---|---|---|---|
| **A** | `ist_day` | 09:30 → 17:10, same-day expiry | 7.7h | ends the cycle on a double stop-out |
| **B** | `full_cycle` | 17:35 → 17:10 next day | 23.6h | ends the cycle on a double stop-out |
| **C** | `continuous` | 17:35 → 17:10 next day | 23.6h | **re-enters** and keeps going |

B and C share a clock and differ only in temperament. B asks *what does one
strangle per expiry earn*; C asks *what does always being short earn*.

**PAPER ONLY.** No API key is needed. The executor's live path raises rather than
being merely switched off, so a config typo cannot send an order.

---

## Run it

```bat
REM one-time, from an ADMIN PowerShell — survives reboots, restarts on failure
powershell -ExecutionPolicy Bypass -File tools\install_tasks.ps1
```

```bat
REM or by hand
run_collector.bat     REM the chain archive — start this FIRST and never stop it
run_engine.bat        REM the three paper books
```

```bat
REM the answer, any time
.venv\Scripts\python.exe live_trading_options\delta_btc\tools\compare.py
.venv\Scripts\python.exe live_trading_options\delta_btc\tools\compare.py --csv month.csv
```

---

## ⚠️ The collector is not optional

Delta serves candle history for **live contracts only**. Ask an expired option for
its candles and you get an empty list — verified, not assumed. Every daily option
this strategy trades is expired within 24 hours, so **there is no way to backtest
this on data we did not capture ourselves.** Every minute the collector is not
running is history that cannot be bought back at any price.

One call a minute to `/v2/tickers` returns the whole BTC chain — mark, both sides
of the touch with sizes, mark/bid/ask IV, full greeks, OI and spot — for 3 of the
20,000 rate-limit units per 5 minutes.

Chunks are written immutably to `data/chain_archive/{date}/chunk_{HHMMSS}.parquet`,
so a crash loses at most one unflushed buffer and can never corrupt what is
already on disk.

---

## What changed from the NSE version, and why

| | NSE (NIFTY/SENSEX) | Delta (BTC) |
|---|---|---|
| Strike grid | uniform 50 / 100 | **non-uniform**: 100 near ATM, then 200, 500, 1000 |
| Clock | exchange session | **a choice** — the three profiles |
| Money | ₹, `qty = lots × lot_size` | **USD**, `contracts × 0.001 BTC` |
| Stops | SL-limit + watchdog (Zerodha blocks SL-M on options) | native **stop-market** with a `mark_price` trigger |
| Feed | Kite WebSocket | REST poll of the full chain |

The selector was **rewritten**, not reconfigured: the old one walked OTM levels
arithmetically (`atm + n × interval`), which on Delta's grid would skip listed
strikes and invent ones that do not exist. It now walks the strikes the exchange
actually lists, which also makes "never sell ATM" a property of the data structure
rather than a check someone can forget to call.

---

## Calibration — the targets are measured, not guessed

All three profiles enter at the **same moneyness** (delta ≈ 0.14, where the NSE
0-DTE book sells), measured live on 2026-09-04 with BTC at 80,819:

| Expiry | delta 0.14 CE | delta 0.14 PE | → target |
|---|---|---|---|
| today (~8h left at A's entry) | $65 | $59 | **A: $70** |
| tomorrow (~24h at B/C's entry) | $169 | $142 | **B/C: $150** |

Matching *premium* instead of delta would have put A three strikes from the money —
under 0.5% OTM on an asset that moves 1–2% in five hours — while B/C sat 3% out.
The comparison would then have measured moneyness, not session length.

Stops are **2× the target** throughout, exactly as NIFTY 20/40 and SENSEX 40/80.

Re-measure if BTC implied vol moves materially.

---

## Deliberately NOT varied in v1

- **The stop-tightening schedule.** The NSE book tightens through expiry day
  (40→30→20). Giving that to A but not B/C would confound the experiment — a
  difference in results could come from the schedule rather than the session.
- **Size and moneyness.** All three trade 100 contracts at delta ≈ 0.14. They
  therefore do *not* collect the same credit per cycle (A ≈ $14, B/C ≈ $30),
  because A sells a contract with 8h of life and B/C one with 24h. That is a real
  property of the choice being tested, which is why `compare.py` reports return
  per hour of exposure and per unit of risk alongside absolute P&L.

---

## Reading the results honestly

`compare.py` prints four rankings, because the profiles hold for very different
lengths of time and a single "who won" number would mostly be ranking risk taken:

- **total** — what the book actually made
- **per cycle** — the average outcome of one decision to be short
- **per hour exposed** — the fair way to read 7.7h against 23.6h
- **on risk** — return against worst-case-both-stopped, the real capital committed

It warns when the sample is small, and when the gap between profiles is small
next to the per-cycle noise. Cycles flagged `late_start` (engine started or
restarted mid-cycle) are excluded from the headline — including them would turn B
into a second copy of A on exactly the days the engine was touched.

Costs are modelled on every fill: a paper SELL fills at the **bid** and a BUY at
the **ask**, plus Delta's fee (the lower of 0.01% of notional and 3.5% of premium,
per side). This strategy re-legs every time the 2× rule fires and the three
profiles trade different numbers of times, so filling at the mark would not just
flatter them all — it could reverse the ranking.

---

## Tests

```bat
.venv\Scripts\python.exe live_trading_options\delta_btc\core\test_selector.py    REM 37
.venv\Scripts\python.exe live_trading_options\delta_btc\core\test_sessions.py    REM 64
.venv\Scripts\python.exe live_trading_options\delta_btc\live\dry_run.py          REM 106
```

207 offline tests, no network. The dry run drives the state machine through a
synthetic chain and a scripted clock: both hard invariants, the 2× adjustment,
window-once semantics, single and double stop-outs, the A/B-vs-C split, max loss,
square-off, midnight rollover, restart resume, and a full day minute by minute
with the never-naked invariant checked at every step.

---

## Layout

```
delta_btc/
├── engine.py              three controllers, one poll loop
├── config/parameters.json profiles + shared params (heavily commented)
├── core/
│   ├── api.py             Delta REST surface (public endpoints only)
│   ├── chain.py           one expiry, kept current; mark vs touch kept apart
│   ├── selector.py        strike selection over a LISTED grid
│   ├── sessions.py        the three profiles — the experiment
│   └── shared.py          lock ports, IST clock
├── live/
│   ├── controller.py      the state machine, one per profile
│   ├── position.py        Leg / Position, USD via contract value
│   ├── executor.py        paper fills at the touch; live path RAISES
│   └── dry_run.py         106-test offline harness
├── tools/
│   ├── collect_chain.py   the archive — run this forever
│   ├── compare.py         the month's answer
│   └── install_tasks.ps1  scheduled tasks so it survives reboots
└── data/
    ├── chain_archive/     immutable 1-min chunks
    ├── results/           cycles.jsonl + per-profile equity
    └── live_state/        snapshots + restart resume files
```

---

## Known limitations

- **No live order path.** By design in v1. When built: signed REST client, own
  `client_order_id` prefix as the own-book scope, rehearsed on testnet
  (`cdn-ind.testnet.deltaex.org`) before real money.
- **No margin model.** Paper does not check whether Delta would actually have
  had the margin. The NSE book lost a morning to exactly this
  (see [[Live Order Safety]]) — it must be added before going live.
- **Depth is not walked.** At 100 contracts against ~7,000 on the touch the whole
  order rests inside level one, so a depth model would be inventing precision the
  size does not need. Revisit if size grows.
- **Archive grows ~100 MB/day** at full chain. A month is ~3 GB.

## Links

- [[Delta Neutral Strangle]] — the NSE parent
- [[Live Order Safety]] — read before any live order path is built
- [[Trading System]] · [[Strategy Tracker]]
