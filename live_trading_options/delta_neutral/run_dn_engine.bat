@echo off
REM ── Delta-Neutral Strangle engine launcher ──────────────────────────────
REM Run by the Windows scheduled task "DeltaNeutralEngine" at 09:20 on weekdays,
REM and by the dashboard watchdog (_ensure_dn_running) if it dies mid-day.
REM
REM USES THE VENV PYTHON. This is the opposite of the local dev machine, where
REM fyers_apiv3 sits on system python — on the VPS the venv is the interpreter
REM that has pandas / kiteconnect, and every other VPS launcher (dashboard,
REM fyers_auto_login, zerodha_auto_login, V2 engine) uses it too. Launching this
REM with bare `python` crashed on ModuleNotFoundError at the first VPS run.
REM
REM The engine takes market data from KITE, not Fyers, so it never competes with
REM the VWAP strangle's Fyers socket.
REM
REM Task created with:
REM   schtasks /Create /TN DeltaNeutralEngine /TR "<this file>" ^
REM     /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:20 /RL HIGHEST /F
REM Hardened afterwards: start-when-missed, restart 3x/1min on failure, and NO
REM execution time limit (that setting once killed a live engine mid-session).

REM
REM RELAUNCH LOOP. If the engine dies it is started again here, in ~5 seconds,
REM rather than waiting for the dashboard's watchdog — which only looks every
REM couple of minutes, and cannot look at all while the dashboard is itself being
REM restarted. On 2026-09-22 that gap plus a CPU-bound box meant 1 to 2.5 minutes
REM blind per kill, nine times. Exit codes:
REM   0  = deliberate: market closed, duplicate instance, no Kite session -> stop
REM   !0 = died: killed, tick-stall self-exit, crash                     -> relaunch
REM Delay uses ping because `timeout` fails without a console (scheduled task).
REM To STOP the engine for good use live_trading_options\tools\restart_dn.ps1 -Stop;
REM killing only the python process now just gets it relaunched.

cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
set /a TRIES=0
:run
.venv\Scripts\python.exe -u live_trading_options\delta_neutral\engine.py >> logs\dn_engine.log 2>&1
set RC=%ERRORLEVEL%
if "%RC%"=="0" goto :done
set /a TRIES+=1
if %TRIES% GEQ 60 goto :done
echo   [launcher] %date% %time% engine exited rc=%RC% - relaunching in 5s (attempt %TRIES%) >> logs\dn_engine.log
ping -n 6 127.0.0.1 >nul
goto :run
:done
