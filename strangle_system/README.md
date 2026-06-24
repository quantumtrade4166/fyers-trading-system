# strangle_system — Nifty/Sensex Short-Strangle Decision Engine

Answers one question each morning: **is today a positive-EV day to sell a
Nifty/Sensex index strangle?** The edge is the Variance Risk Premium (VRP):
sell only when implied vol exceeds the realized vol that subsequently arrives,
and no large move breaches the wings.

Fail-safe by design: when a signal is missing, stale, or uncertain, the system
defaults to **no-trade**.

## Layers (data flows downward)
| Layer | Module | Status |
|------|--------|--------|
| 0 Data | `data/chain_collector.py`, `data/chain_loader.py` | ✅ built |
| 1 Volatility edge (VRP) | `layers/l1_volatility.py` | ✅ built |
| 2 Event/regime guardrails | `layers/l2_guardrails.py` | ⬜ Phase 2 |
| 3 Term structure & skew | `layers/l3_term_structure.py` | ⬜ Phase 3 |
| 4 GEX regime (gated) | `layers/l4_gex.py` | ⬜ Phase 4 |
| 5 Decision + sizing | `layers/l5_decision.py` | ⬜ Phase 5 |

Shared contracts: `signals.py` (typed `@dataclass`). All tunables: `config.py`.

## Data model
Fyers `optionchain` with `greeks=1` gives **real per-strike IV + greeks**, so
L1 needs no Black-Scholes inversion and L4 GEX gets gamma directly.

Snapshots: `data/chain_snapshots/{UNDERLYING}/{YYYY-MM-DD}.parquet`
(columns: `chain_collector.SNAPSHOT_COLUMNS`). Manifest:
`data/chain_snapshots/chain_manifest.json`.

**Forward-accumulate only** (user decision): no historical IV source. The VRP
edge cannot be *validated* until ~40+ usable daily snapshots accrue. Until then
L1 emits a live VRP daily (to be paper-logged) and `vrp_validation.py` reports
`INSUFFICIENT HISTORY`.

## Run
```bash
# Collect today's chain snapshot (NIFTY + SENSEX). Needs a valid Fyers token.
python -m strangle_system.data.chain_collector              # + Drive push
python -m strangle_system.data.chain_collector --no-drive   # local only
python -m strangle_system.data.chain_collector --probe      # dump raw response

# Inspect snapshots / compute L1 / run the validation gate
python -m strangle_system.data.chain_loader
python -m strangle_system.layers.l1_volatility
python -m strangle_system.backtest.vrp_validation

# Tests
.venv/Scripts/python.exe -m pytest strangle_system/tests/ -q
```

## Deployment (forward accumulation) — TODO on VPS
1. **Lock field mapping**: `--probe` on the VPS (verified once locally 2026-06-24; greeks nested under `"greeks"`).
2. **Drive creds**: `config/gdrive_credentials.json` (service account), share a Drive folder with the SA email. Then drop `--no-drive`.
3. **Schedule** 3 Task Scheduler runs at 15:05 / 15:15 / 15:25 IST (multi-capture, latest-wins). Market-live capture → real bid/ask.
4. **Health alert**: notify if a day's snapshot is missing or Drive upload failed (manifest-driven).

## Known data notes
- SENSEX BSE far-strike **bid/ask often `0`** (thin quotes) — watch for slippage modeling.
- SENSEX **spot history not in the local 5-min tree** → L1 RV/VRP unavailable for SENSEX until backfilled (Fyers daily `history` can backfill).
