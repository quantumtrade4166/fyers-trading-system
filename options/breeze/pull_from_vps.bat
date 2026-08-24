@echo off
REM ============================================================
REM  Pull the Breeze options download from the VPS to local G:
REM
REM  data/ is gitignored, so the download cannot travel by git.
REM  The VPS is where the multi-week download runs (uninterrupted);
REM  backtesting happens locally, so the parquets have to come back.
REM
REM  Safe to run any time - scp overwrites whole parquet files, and
REM  the downloader rewrites them atomically on the VPS side.
REM ============================================================
set VPS=Administrator@144.79.166.103
set VPS_DIR=C:/Users/Administrator/Desktop/fyers_data_pipeline_git/data/BREEZE_OPTIONS
set LOCAL_DIR=G:\fyers_data_pipeline\data\BREEZE_OPTIONS

echo ================================
echo  Breeze data pull from VPS
echo ================================

if not exist "%LOCAL_DIR%" mkdir "%LOCAL_DIR%"

echo Pulling NIFTY parquets...
scp -r "%VPS%:%VPS_DIR%/NIFTY" "%LOCAL_DIR%\"

echo Pulling BSESEN parquets...
scp -r "%VPS%:%VPS_DIR%/BSESEN" "%LOCAL_DIR%\"

echo Pulling download manifest...
scp "%VPS%:%VPS_DIR%/download_manifest.json" "%LOCAL_DIR%\download_manifest.json"

echo.
echo Done: %LOCAL_DIR%
echo Timestamp: %date% %time%
echo.
echo Check what landed:
echo    .venv\Scripts\python.exe -m options.breeze.status
