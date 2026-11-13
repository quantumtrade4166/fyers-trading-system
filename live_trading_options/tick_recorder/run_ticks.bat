@echo off
REM -- NIFTY + SENSEX options tick recorder launcher ---------------------------------------------
REM Run by the scheduled task "TickRecorder": at boot and every 5 minutes. Outside 09:00-15:40 IST
REM Mon-Fri, or when one is already running (port 47693), the recorder exits at once with code 0 and
REM this launcher ends. Any other exit = crash -> restarted here with a back-off (10 s, 30 s, 60 s,
REM then 5 min) so a broken recorder never logs in to Kotak in a tight loop.

setlocal EnableDelayedExpansion
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
set LOG=logs\tick_recorder_stdout.log
set FAILS=0

:run
call :now T0
.venv\Scripts\python.exe -u -m live_trading_options.tick_recorder.service >> %LOG% 2>&1
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
