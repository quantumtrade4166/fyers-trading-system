# Breeze Options Downloader

ICICI Breeze pipeline for NIFTY and SENSEX options history, kept entirely
separate from the Fyers data: everything lands under `data/BREEZE_OPTIONS/`,
and `data/NSE_NIFTY_OPTIONS/` is never written to.

Built because the existing dataset is **front-week only**, which is what has
[[Supertrend Credit Spread]] parked — 72% of its signals fire at 0–3 DTE and
can't be traded without next-week contracts.

---

## What we established empirically (2026-08-11)

Verified against the live account, not from docs:

| Question | Answer |
|---|---|
| 1-second data available? | **Yes**, NIFTY and SENSEX |
| How far back is 1-second? | **2023** (2021/2022 return nothing) |
| Next-week expiry available? | **Yes, 2021–2026** — unblocks credit spreads |
| SENSEX / BFO available? | **Yes** — `exchange_code="BFO"`, `stock_code="BSESEN"` |
| 1-minute history depth | 2021 → present |
| Intervals that work | `1second`, `1minute`, `5minute`, `30minute` |
| Rows per contract-day (1-sec) | ~22,258 — options trade nearly every second |
| Calls per contract-day (1-sec) | ~22–24 |
| Observed throughput | ~30 calls/min (latency-bound, not quota-bound) |

### The expiry calendar was wrong

`data/NSE_NIFTY_OPTIONS/expiry_calendar.csv` was derived from OI craters, a
heuristic that produced **31 phantom expiry dates out of 289** (~11%) — dates
where no contract ever existed, e.g. `2022-06-24` (Friday; the real expiry was
Thursday the 23rd). All 289 were tested against Breeze; the calendar was
replaced with the validated 263-date version and the original preserved as
`expiry_calendar_ORIGINAL_backup.csv`.

**Open item:** `backtesting/options_credit_spread/` used the old calendar for DTE
and trade construction, so its published CAGR 9.24% / DD −20.25% should be
treated as suspect until re-run.

---

## The binding constraint

| Limit | Value |
|---|---|
| Candles per request | 1,000 (hard) |
| API calls | 100/min, **5,000/day** |

One 1-second contract-day is ~22,258 candles → 23 requests. The full scope
(ATM±10, front+next, both indices = 168 contracts) is **4,032 calls per single
day of data**, against a 5,000/day budget.

| Scope | Calls/data-day | Download time |
|---|---|---|
| FULL: ATM±10, front+next, 3.4yr, both | 4,032 | ~692 days |
| ATM±10, front only, 1yr, both | 2,016 | ~102 days |
| ATM±3, front only, 1yr, both | 672 | ~34 days |
| Targeted (selected strikes only), 3.4yr, both | 192 | ~33 days |
| **1-minute**, ATM±10, front+next, 5yr, NIFTY | 84 | ~21 days |

1-minute is 1 call per contract-day — **24× cheaper** than 1-second for the same
contract. That ratio is the whole story.

The only real lever is a quota increase: `breezeapi@icicisecurities.com`.
The published limits were themselves an increase, so there is a precedent.

---

## Setup

App registration at https://api.icicidirect.com/apiuser/home:

- **Redirect URL:** `https://127.0.0.1`
- **Primary IP:** the VPS static IP (SEBI mandate since 1 Apr 2026; changeable
  only once per week)
- **Secondary IP:** your home IP — confirmed that Breeze honours the secondary
  for historical-data calls

Credentials go in `deployment/.env` (git-ignored):

```
BREEZE_API_KEY=...
BREEZE_API_SECRET=...
```

Note: the API key is 32 chars. A leading `-` copied from "api key - xxx" will
fail with `Public Key does not exist.` — the login page issues a session token
regardless, so a successful login does **not** prove the key is right.

## Daily login (manual — no headless flow exists)

Tokens expire every day, and Breeze has no TOTP login like `auth/auto_login.py`.

```bash
python -m options.breeze.session --login       # prints the URL
python -m options.breeze.session --set-token NNNNNNNN
python -m options.breeze.session --check
```

The redirect lands on `https://127.0.0.1/?apisession=NNNNNNNN` and shows
"This site can't be reached" — that's expected, the token is in the address bar.

---

## Commands

```bash
# Capability probe — run once, ~25 calls
python -m options.breeze.probe            # everything
python -m options.breeze.probe --quick    # just 1-sec depth + next-week expiry

# SENSEX support
python -m options.breeze.probe_sensex                 # is BFO available?
python -m options.breeze.sensex_calendar --spot-only  # daily spot (3 calls)
python -m options.breeze.sensex_calendar              # + discover expiries

# Expiry calendar validation (NIFTY)
python -m options.breeze.validate_calendar --suspicious --repair
python -m options.breeze.validate_calendar --all --skip-validated --repair
python -m options.breeze.validate_calendar --find-missing

# Download
python -m options.breeze.downloader --dry-run --start 2023-01-01 --end 2026-05-31
python -m options.breeze.downloader --start 2023-01-01 --end 2026-05-31 \
    --interval 1second --expiries 2 --strikes 10
python -m options.breeze.downloader --stock-code BSESEN --exchange-code BFO ...

# Local-only, spends no budget
python -m options.breeze.status          # what's on disk
python -m options.breeze.reconcile --fix # drop manifest entries with no data
```

`options\breeze\resume_download.bat` does check-session → reconcile → resume →
status in one double-click.

---

## Durability

A months-long unattended download has two failure modes that corrupt data
silently. Both are handled:

1. **Crash between fetch and write.** Parquet is written *before* the manifest
   commits, so the manifest can never claim data that isn't on disk. Without
   this, a sleep or kill leaves the resume skipping lost contract-days.

2. **A quota wall disguised as empty responses.** If the server returns HTTP 200
   with no payload instead of an error, naive code marks thousands of
   contract-days as legitimately "empty" and never retries them. Trailing empty
   results are therefore held back until a non-empty proves the stream healthy,
   and `MAX_CONSECUTIVE_EMPTY = 60` trips a circuit breaker that discards them.

`--ignore-daily-cap` disables the client-side 5,000 ceiling so the *server*
becomes the limit — used to discover the real enforced quota.

---

## Output schema

`data/BREEZE_OPTIONS/{stock_code}/{year}/ohlcv_{interval}.parquet`

`datetime, expiry, strike_price, option_type, open, high, low, close, volume,
oi, date, stock_code`

The `expiry` column is new — the Fyers dataset didn't need one, being front-week
only. Re-downloading is idempotent: rows dedupe on
`(datetime, expiry, strike_price, option_type)`.
