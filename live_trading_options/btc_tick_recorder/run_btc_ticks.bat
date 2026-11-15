@echo off
REM -- BTC option chain tick recorder launcher (Delta Exchange, 24/7) --------------------------------
REM Run by the scheduled task "BtcTickRecorder": at boot and every 5 minutes. If one is already running
REM (port 47696) the recorder exits at once with code 0 and this launcher ends. Any other exit = crash
REM -> restarted here with a back-off (10 s, 30 s, 60 s, then 5 min).

setlocal EnableDelayedExpansion
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
set LOG=logs\btc_tick_recorder_stdout.log
set FAILS=0

:run
call :now T0
.venv\Scripts\python.exe -u -m live_trading_options.btc_tick_recorder.service >> %LOG% 2>&1
set RC=!ERRORLEVEL!
call :now T1
set /a RAN=T1-T0
if "!RC!"=="0" exit /b 0
if !RAN! GEQ 300 set FAILS=0
set /a FAILS+=1
set WAIT=10
if !FAILS! GEQ 2 set WAIT=30
if !FAILS! GEQ 3 set WAIT=60
if !FAILS! GEQ 5 set WAIT=300
echo [%date% %time%] launcher: RECORDER STOPPED code !RC! after !RAN! s - restart !FAILS! in !WAIT! s >> %LOG%
set /a PINGS=WAIT+1
ping -n !PINGS! 127.0.0.1 >nul
goto run

:now
for /f %%t in ('powershell -NoProfile -Command "[DateTimeOffset]::Now.ToUnixTimeSeconds()"') do set %1=%%t
exit /b 0
