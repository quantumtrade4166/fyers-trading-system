@echo off
REM ============================================================================
REM  BTC futures arbitrage monitor — Delta India vs Binance, READ-ONLY.
REM  Public market data only, no keys, no orders. Lock port 47664 blocks a duplicate.
REM  Output: crypto\btc_arbitrage\data\  and  crypto\btc_arbitrage\logs\
REM ============================================================================
setlocal
set "REPO=%~dp0..\.."
cd /d "%REPO%"
set PYTHONIOENCODING=utf-8
"%REPO%\.venv\Scripts\python.exe" "%~dp0main.py"
