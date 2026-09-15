@echo off
REM DualMom live service — its OWN process, separate from the dashboard.
REM Restarting this NEVER touches the live Vwap Strangle, so it is safe even
REM while the market is open.
REM
REM SUPERVISOR LOOP: on 2026-09-10 the service ran for 10 hours and then exited
REM with code 1 and no Python traceback — killed from outside, cause unknown.
REM A strategy service that can place orders must not depend on that never
REM happening again, so this relaunches it and records every restart.
cd /d "%~dp0"
if not exist logs mkdir logs

:loop
echo [%date% %time%] starting >> logs\dualmom_service.log
.venv\Scripts\python.exe -m deployment.dualmom_service >> logs\dualmom_service.log 2>&1
echo [%date% %time%] EXITED errorlevel=%errorlevel% — restarting in 15s >> logs\dualmom_service.log
REM a crash loop must not spin: 15s between attempts
timeout /t 15 /nobreak > nul
goto loop
