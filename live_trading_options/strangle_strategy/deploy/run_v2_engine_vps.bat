@echo off
REM V2 tick engine (Vwap Strangle) - STANDALONE, own WebSocket.
REM Start during market hours AFTER 9:20 (needs the day's strike selection).
REM Writes {date}_{index}_V2.json. Self-exits after 15:35.
REM
REM RELAUNCH LOOP (added 2026-09-22). If the engine dies it is started again here
REM in ~5 seconds, instead of waiting for the dashboard's _ensure_v2_running - which
REM only looks every couple of minutes and cannot look at all while the dashboard
REM itself is restarting. That day the dashboard was redeployed ten times during
REM market hours and each time the engine sat dead until the watchdog noticed.
REM
REM Exit codes decide it:
REM   0  = stopping on purpose: market closed, duplicate instance, token invalid,
REM        no strikes cached yet (the watchdog starts it again later)   -> stop
REM   !0 = died: killed, tick-stall self-exit, crash                     -> relaunch
REM (The engine's own tick-stall path also runs `schtasks /Run` before exiting; with
REM this launcher still alive that call is ignored - the task is IgnoreNew - so
REM there is never a second engine.)
REM Delay uses ping because `timeout` fails without a console (scheduled task).
REM To stop it for good: end the task FIRST, then the python process - killing only
REM the python now just gets it relaunched.
cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
set /a TRIES=0
:run
.venv\Scripts\python.exe live_trading_options\strangle_strategy\live_tick_engine.py >> logs\strangle_v2.log 2>&1
set RC=%ERRORLEVEL%
if "%RC%"=="0" goto :done
set /a TRIES+=1
if %TRIES% GEQ 60 goto :done
echo   [launcher] %date% %time% V2 engine exited rc=%RC% - relaunching in 5s (attempt %TRIES%) >> logs\strangle_v2.log
ping -n 6 127.0.0.1 >nul
goto :run
:done
