@echo off
REM Push dualmom_live_api.py fix to VPS and restart DualMom service
setlocal

set "SSH=ssh -o BatchMode=yes -o ConnectTimeout=20 Administrator@103.49.131.58"
set "SCP=scp -o BatchMode=yes"

echo Pushing dualmom_live_api.py to VPS...
%SCP% "G:\fyers_data_pipeline\deployment\dualmom_live_api.py" "Administrator@103.49.131.58:C:/trading/fyers_data_pipeline/deployment/dualmom_live_api.py"
if errorlevel 1 (
    echo *** SCP FAILED ***
    pause
    exit /b 1
)
echo   copied.

echo Restarting DualMom service on VPS...
%SSH% "powershell -NoProfile -Command ^
  $p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*deployment.dualmom_service*' }; ^
  foreach ($x in $p) { Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue }; ^
  Start-Sleep 3; ^
  schtasks /Run /TN DualMomService | Out-Null; ^
  Write-Host 'service_restarted'"

echo.
echo Done. Check the dashboard - Kotak stats should no longer blank on timeouts.
pause
