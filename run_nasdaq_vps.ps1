$ErrorActionPreference = "Continue"
$log = "C:\Users\Administrator\Desktop\fyers_data_pipeline_git\forex\logs\nasdaq_download.log"
New-Item -ItemType Directory -Force -Path "C:\Users\Administrator\Desktop\fyers_data_pipeline_git\forex\logs" | Out-Null
Set-Content -Path $log -Value "=== Nasdaq Download Started: $(Get-Date) ===" -Encoding UTF8
cd "C:\Users\Administrator\Desktop\fyers_data_pipeline_git"
& ".venv\Scripts\python.exe" "forex\download_nasdaq_1m.py" *>&1 | Tee-Object -FilePath $log -Append
"EXIT_CODE: $LASTEXITCODE" | Add-Content -Path $log -Encoding UTF8
