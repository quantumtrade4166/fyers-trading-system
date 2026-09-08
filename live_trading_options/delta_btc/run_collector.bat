@echo off
REM ============================================================================
REM  BTC option-chain collector — runs forever, one poll a minute.
REM
REM  This is the piece that CANNOT be missed. Delta serves candle history for
REM  live contracts only; an expired option returns nothing, and every daily
REM  option is expired within 24 hours. History not captured as it happens
REM  cannot be bought back at any price.
REM
REM  Paths are derived from THIS FILE's location (%~dp0 -> ...\delta_btc\), so
REM  the same script works on the local G: drive and on the VPS under
REM  C:\Users\Administrator\Desktop\fyers_data_pipeline_git. Hardcoding G:\ here
REM  is what would make the deploy silently do nothing on the VPS.
REM
REM  Needs pandas + pyarrow in the venv (the engine does not — it is pure stdlib).
REM  Safe to double-launch: a loopback lock (port 47654) means the duplicate
REM  stands down instead of doubling every archive row.
REM ============================================================================
setlocal
set "REPO=%~dp0..\.."
cd /d "%REPO%"
"%REPO%\.venv\Scripts\python.exe" "%~dp0tools\collect_chain.py"
