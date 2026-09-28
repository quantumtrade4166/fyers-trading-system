@echo off
REM ============================================================================
REM  BTC VWAP Strangle — ist_day + full_expiry, PAPER ONLY.
REM  NIFTY Vwap Strangle rules on Delta Exchange BTC daily options.
REM  Resumes any cycle in flight on restart. Lock port 47657 blocks a duplicate.
REM ============================================================================
setlocal
set "REPO=%~dp0..\.."
cd /d "%REPO%"
"%REPO%\.venv\Scripts\python.exe" "%~dp0vwap_engine.py"
