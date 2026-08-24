@echo off
REM ===================================================================
REM  Resume the Breeze 1-second download.
REM
REM  Breeze session tokens expire DAILY and there is no headless login,
REM  so each morning you must log in first:
REM
REM    1) .venv\Scripts\python.exe -m options.breeze.session --login
REM    2) log in in the browser, copy the apisession=NNNN value
REM    3) .venv\Scripts\python.exe -m options.breeze.session --set-token NNNN
REM    4) run this file
REM
REM  The download is resumable: the manifest records every contract-day,
REM  so this picks up exactly where it stopped and re-spends no calls.
REM ===================================================================

cd /d G:\fyers_data_pipeline

echo Checking session...
.venv\Scripts\python.exe -m options.breeze.session --check
if errorlevel 1 (
    echo.
    echo Session invalid or expired - log in first:
    echo    .venv\Scripts\python.exe -m options.breeze.session --login
    pause
    exit /b 1
)

echo.
echo Reconciling manifest against files on disk...
.venv\Scripts\python.exe -m options.breeze.reconcile --fix

echo.
echo Resuming NIFTY 1-second download...
.venv\Scripts\python.exe -u -m options.breeze.downloader ^
    --start 2023-01-01 --end 2026-05-31 ^
    --interval 1second --expiries 2 --strikes 10 ^
    --ignore-daily-cap

echo.
echo Run ended. Current dataset status:
.venv\Scripts\python.exe -m options.breeze.status
pause
