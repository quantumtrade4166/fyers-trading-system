@echo off
REM ── Delta-Neutral preflight ─────────────────────────────────────────────
REM Runs at 09:15 on weekdays, five minutes before the engine starts, and writes
REM the day's resolved rules to logs\dn_preflight.log. Read-only: it never places
REM an order and never opens a socket.
REM
REM Exists so the morning state is captured automatically — what DTE each index
REM is on, the entry target, the stop schedule, and whether the strategy is armed
REM — without anyone having to run anything by hand.

cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
echo ================================================== >> logs\dn_preflight.log
.venv\Scripts\python.exe -u live_trading_options\delta_neutral\tools\preflight.py >> logs\dn_preflight.log 2>&1
