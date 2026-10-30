@echo off
REM -- MCX Desk (MCX options terminal, Kotak P) launcher ----------------------------------
REM Run by the scheduled task "McxTerminal": at boot and every 5 minutes (a running engine makes the
REM extra start a no-op: the task ignores new instances and port 47680 admits one engine only).
REM The engine exits itself at 08:45 IST each new day (exit code 3); this launcher then ends and the
REM next 5-minute start brings a fresh one (new Kotak login, today's contract list, open paper
REM positions carried over).
REM CRASH RESTART (30-Oct): any OTHER exit = crash -> restarted here in 10 s instead of waiting up to
REM 5 min for the task. Repeated quick crashes back off (10 s, 30 s, 60 s, then 5 min) so a broken
REM engine does not log in to Kotak in a tight loop. A run of 5+ min resets the back-off.
REM Served on 127.0.0.1:47680 -> Cloudflare tunnel -> https://mcx.trading.contact (Access + own lock).

setlocal EnableDelayedExpansion
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
set LOG=logs\mcx_terminal_stdout.log
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
