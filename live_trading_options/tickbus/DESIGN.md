# Tick Bus — shared Kite feed platform

**Problem.** Zerodha allows **max 3 WebSocket connections per API key**. We already run 3
(`delta_neutral`, `nifty_st_directional`, `nifty_pivot`), all on the one `KITE_API_KEY`.
A 4th live ticker (for the VWAP strangle, or any new strategy) would be refused and would
knock an existing engine offline. Fyers is no longer an option (paid/quota-capped).

**Solution.** One process owns the single real `KiteTicker` and re-broadcasts ticks to any
number of local strategy processes over a `127.0.0.1` socket. Strategies then share ONE
Zerodha connection, and new strategies attach as local subscribers — never a new broker
socket. We go from 3 sockets to 1, with 2 spare.

## Components
- `protocol.py` — length-prefixed **pickle** frames over localhost TCP. Pickle so consumers
  get the EXACT tick dicts (datetimes intact) KiteTicker delivers → zero behavioural drift.
  Localhost-only bind makes pickle safe (only this machine's own processes connect).
- `bus_server.py` (`TickBus`) — owns the real ticker, maintains the token→highest-mode union,
  fans each tick to the consumers that subscribed it.
- `bus_client.py` (`BusTicker`) — **drop-in `KiteTicker` replacement**. Same ctor, same
  `on_ticks/on_connect/on_close/on_error`, same `subscribe` / `set_mode` / `connect`, same
  `MODE_*`. Auto-reconnects to the bus and re-subscribes.
- `run_bus.py` — the live bus process (reads KITE_API_KEY + today's zerodha_token.json).
- `tests/test_tickbus.py` — offline proof (FakeTicker, no broker).

## Reliability invariants (why it is safe for live money)
1. The Kite read loop never does socket IO and never raises — it only drops ticks onto
   per-consumer bounded queues. A slow/stuck/dead consumer can't back-pressure the feed
   or starve peers. Queue full → drop OLDEST (feed stays live).
2. Each consumer has its own queue + sender thread; a socket error drops only that consumer.
3. Every Kite (re)connect re-subscribes the full union (Kite forgets subs on reconnect).
4. `BusTicker` auto-reconnects + re-subscribes, so a bus restart is a brief gap, not a dead
   engine — same resilience KiteTicker gives against a Zerodha drop.
5. A consumer whose `on_ticks` throws only aborts that one callback (caught), never the bus.

Tests cover: union/highest-mode, per-consumer fan-out, datetime preservation, consumer
fault isolation, client auto-reconnect across a full bus restart. **3/3 passing.**

## Migrating an engine = one line (and instantly reversible)
```python
# from kiteconnect import KiteTicker          ->  from tickbus.bus_client import BusTicker
# kws = KiteTicker(api_key, access_token)     ->  kws = BusTicker(api_key, access_token)
```
Everything else (on_connect/subscribe/set_mode/on_ticks) is unchanged. Revert by changing
the line back — the engine reopens its own Zerodha socket.

## Rollout (never exceed 3 live sockets; live-money DN migrated LAST)
- **Stage 0 — DONE:** build + offline tests (no broker). ✅
- **Stage 1:** bus goes live serving **strangle (new) + Pivot (paper)**; Pivot → `BusTicker`
  frees its socket → total still 3. Prove in production at zero money risk.
- **Stage 2:** migrate **ST (paper)** → 2 sockets.
- **Stage 3:** migrate **DN (live)** last, after days of proven bus uptime → 1 socket.

Each stage is independently reversible (flip the one import line back).

## Ops (Stage 1+)
- Windows task `TickBus` (SYSTEM, boot + 5-min watchdog), launcher relaunches on non-zero exit.
- Heartbeat: `tickbus/data/bus_heartbeat.txt` (mtime every 2s) — watchdog restarts if stale.
- Port `127.0.0.1:47670`.
