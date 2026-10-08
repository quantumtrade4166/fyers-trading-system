@echo off
REM Push the latest dualmom_live_api.py and restart the DualMom service
setlocal

set VPS=Administrator@144.79.166.103
set SRC=%~dp0deployment\dualmom_live_api.py
set DEST=C:/Users/Administrator/Desktop/fyers_data_pipeline_git/deployment/dualmom_live_api.py

echo Pushing fix...
scp -o BatchMode=yes "%SRC%" "%VPS%:%DEST%"
if errorlevel 1 (
    echo COPY FAILED
    pause
    exit /b 1
)
echo Copied.

echo Restarting DualMom service...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_dualmom_vps2.ps1"
endlocal
