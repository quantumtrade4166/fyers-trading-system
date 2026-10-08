@echo off
REM ============================================================================
REM  Binance vs Delta BTC options liquidity recorder (read-only, public data).
REM  Lock port 47662 blocks a duplicate. Output: data\liquidity_compare\
REM ============================================================================
setlocal
set "REPO=%~dp0..\.."
cd /d "%REPO%"
"%REPO%\.venv\Scripts\python.exe" "%~dp0tools\record_binance_delta.py"
