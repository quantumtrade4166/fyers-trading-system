"""
strangle_system — Nifty/Sensex short-strangle decision engine.

Layered architecture (data flows strictly downward):
  data/   — option-chain snapshot collection + loading
  layers/ — L1 volatility edge (VRP) → L2 guardrails → L3 term/skew
            → L4 GEX (gated) → L5 decision + sizing
  backtest/ — VRP validation, GEX validation (gate), full strangle backtest

See README.md and the build spec for phase order. Build edge (L1) first;
speculative refinements (L4 GEX) are gated behind validation backtests.
"""
