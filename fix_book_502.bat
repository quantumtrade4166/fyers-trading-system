@echo off
setlocal
set VPS=Administrator@144.79.166.103

echo [1/2] Pushing dualmom_live_api.py...
scp -o BatchMode=yes "%~dp0deployment\dualmom_live_api.py" "%VPS%:C:/Users/Administrator/Desktop/fyers_data_pipeline_git/deployment/dualmom_live_api.py"
if errorlevel 1 (
    echo SCP FAILED
    pause
    exit /b 1
)
echo Copied.

echo [2/2] Restarting DualMom service...
ssh -o BatchMode=yes -o ConnectTimeout=20 "%VPS%" "powershell -NoProfile -Command "\"$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'dualmom_service' }; foreach ($x in $p) { Stop-Process -Id $x.ProcessId -Force }; Start-Sleep 3; schtasks /Run /TN DualMomService\""
echo Done.
endlocal
