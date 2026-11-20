# Order gateway — design (started 19-Nov-2026)

## Why
One Python process (`server.py`) runs everything for a desk: Kotak price feed for the screens, option
chains, charts, news, margin, paper book, recorder, AND the live order path (LiveBook, auto stop-loss,
risk guard, exit timers). Any of the first group stalling (GIL, disk, a slow Kotak call, a burst of chart
requests) froze the exit button too (17-19 Nov: 0.5-35 s freezes; +15k -> +1k on 19-Nov).

## The split
```
 browser ──wss──► SCREEN process (server.py, :47680 MCX / :47682 NSE)          unchanged for the user
                   prices, chains, charts, paper book, news, margin, recorder
                   LIVE actions ──HTTP 127.0.0.1 (keep-alive, ~1 ms)──►
                                                                    ORDER GATEWAY (gateway.py, :47681 / :47683)
                   ◄──── live book / SL / risk / timers pushed (WS) ──   LiveBook + OrderFeed (fills)
                                                                         AutoSL (LIVE) + RiskGuard (LIVE)
                                                                         ExitTimers (LIVE)
                                                                         arm state (LIVE/PAPER)
                                                                         own small Kotak price feed:
                                                                           held + working + SL contracts
                                                                           + the contracts on screen (hint)
                                                                         EMERGENCY page: positions + EXIT ALL
```
* The gateway does nothing else - no charts, chains, news, margin panel, disk-heavy work. Its threads:
  HTTP, Kotak order sends, order-feed, position sync, its small price feed, SL / risk / timer loops.
* Own Kotak login (SessionHolder; a 2nd session on the same key is fine - tested 16-Oct).
* Prices for an order: the gateway's own feed quote if fresh (< 2 s); else the screen's quote sent WITH
  the click (if < 1 s old); else a REST quote (~50-100 ms). Exits always have the REST fallback
  (`_exit_price_fallback`), so a frozen screen never blocks an exit.
* Auto stop-loss, risk guard (MTM stop / target / trail) and exit timers live in the gateway, so they keep
  working while the screen process is frozen or restarting.

## Link
* Screen -> gateway: `POST /op/{name}` JSON, same argument shapes as today's ORDER_OPS; answer = the same
  result dict. Auth: a random secret in `data/gateway_secret_{desk}` (127.0.0.1 only, file readable only on
  the VPS). Owner checks stay in the screen (it has the Cloudflare identity); `mode_set` forwards the
  verified user.
* Gateway -> screen: one websocket `/stream` pushing `{"t":"book"|"sl"|"risk"|"timers"|"note"|"status"}`
  messages; the screen keeps the latest copy and rebroadcasts to browsers exactly as now.
* Screen side: `RemoteLive` (live book interface), `RemoteSL`, `RemoteRisk`, `RemoteTimers` proxies, so
  `server.py` changes only where it builds the LIVE objects.
* Gateway down / not answering: the screen shows a red "ORDERS OFFLINE" banner and refuses LIVE orders
  with a clear message (never a silent hang); PAPER keeps working.

## Emergency page
`https://mcx.trading.contact/emergency` (tunnel path route to the gateway, same Cloudflare Access login,
gateway checks the Access token like `access.py`): live positions, P&L, one big EXIT ALL (typed confirm),
per-instrument exit. Works when the screen process is frozen or down. Phone friendly.

## Files the gateway owns (screen never writes them when the gateway is on)
`live_book_{day}.json`, `risk_live_{day}.json`, live SL state, `live_arm_{day}.json`,
`sched_exits_live_{day}.json` (paper timers stay in the screen's own file).

## Switch and rollback
`config.ORDER_GATEWAY` (env `MCX_ORDER_GATEWAY=1`). Off = today's in-process LiveBook, unchanged.
Rollback = turn it off and restart the screen; the gateway task is simply disabled.

## Rollout
1. Build gateway + proxies + tests with the fake Kotak (EXIT ALL, refused leg, slow cancel, gateway down,
   screen frozen, restart mid-order, two screens).
2. VPS: gateway runs in PAPER-only mode alongside (arm refused) for a day; compare its book with Kotak.
3. NSE desk first (deploy after 15:30), LIVE 1 lot; then MCX (after 23:30).
4. Emergency page + tunnel route.
5. Move remaining background jobs out of the screen (margin, news, recorder writes off the hot path).
