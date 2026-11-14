@echo off
REM -- BTC Desk (Delta Exchange India BTC options terminal, PAPER only) launcher -------------------
REM Same engine code as the MCX Desk, started with TERMINAL_DESK=BTC (config.py): port 47684, data_btc\,
REM logs\btc_terminal.log + logs\btc_audit. Prices from Delta's PUBLIC feed, money in USD. PAPER ONLY:
REM no Delta key is used, no order ever reaches Delta (owner, 15-Nov-2026: live later).
REM Run by the scheduled task "BtcTerminal": at boot and every 5 minutes (a running engine makes the
REM extra start a no-op: the task ignores new instances and port 47684 admits one engine only).
REM The engine exits itself at 17:35 IST, after the daily expiry (exit code 3); the next 5-minute start brings a fresh one.
REM Any OTHER exit = crash -> restarted here with a back-off (10 s, 30 s, 60 s, then 5 min).
REM Served on 127.0.0.1:47684 -> Cloudflare tunnel -> https://btc.trading.contact (Access + own lock).

setlocal EnableDelayedExpansion
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
set TERMINAL_DESK=BTC
set LOG=logs\btc_terminal_stdout.log
set FAILS=0

:run
call :now T0
.venv\Scripts\python.exe -u -m live_trading_options.mcx_terminal.server >> %LOG% 2>&1
set RC=!ERRORLEVEL!
call :now T1
set /a RAN=T1-T0
if "!RC!"=="3" (
    echo [%date% %time%] launcher: daily restart exit ^(code 3^) - task starts the next engine >> %LOG%
    exit /b 0
)
if !RAN! GEQ 300 set FAILS=0
set /a FAILS+=1
set WAIT=10
if !FAILS! GEQ 2 set WAIT=30
if !FAILS! GEQ 3 set WAIT=60
if !FAILS! GEQ 5 set WAIT=300
echo [%date% %time%] launcher: ENGINE STOPPED code !RC! after !RAN! s - restart !FAILS! in !WAIT! s >> %LOG%
set /a PINGS=WAIT+1
ping -n !PINGS! 127.0.0.1 >nul
goto run

:now
for /f %%t in ('powershell -NoProfile -Command "[DateTimeOffset]::Now.ToUnixTimeSeconds()"') do set %1=%%t
exit /b 0
