$ErrorActionPreference = "Continue"
$log = "C:\trading\fyers_data_pipeline\forex\logs\nasdaq_download.log"
New-Item -ItemType Directory -Force -Path "C:\trading\fyers_data_pipeline\forex\logs" | Out-Null
Set-Content -Path $log -Value "=== Nasdaq Download Started: $(Get-Date) ===" -Encoding UTF8
cd "C:\trading\fyers_data_pipeline"
& ".venv\Scripts\python.exe" "forex\download_nasdaq_1m.py" *>&1 | Tee-Object -FilePath $log -Append
"EXIT_CODE: $LASTEXITCODE" | Add-Content -Path $log -Encoding UTF8
