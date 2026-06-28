# vps_backup_to_drive.ps1 -- backs up VPS trade data + config/secrets to Google Drive.
# Runs as Administrator (Google Drive G: is per-user) every 10 min via task VPSBackupToDrive.
# Backs up ONLY irreplaceable data + secrets; code/venv/caches are excluded (code is on GitHub).
$ErrorActionPreference='SilentlyContinue'
$repo='C:\Users\Administrator\Desktop\fyers_data_pipeline_git'
$dest='G:\My Drive\VPS Backup'
$td="$dest\Trade Data"; $cfg="$dest\Config"
New-Item -ItemType Directory -Force -Path $td,$cfg | Out-Null
$rc=@('/R:1','/W:1','/NFL','/NDL','/NP','/NJH','/NJS')
$xd=@('/XD','__pycache__')

# ---- TRADE DATA (state + outputs; no code, no huge regenerable symbol masters) ----
robocopy "$repo\deployment" "$td\deployment" *.json *.csv @rc | Out-Null
robocopy "$repo\strangle_system\flags" "$td\strangle_flags" /E @rc @xd | Out-Null
robocopy "$repo\live_trading_options\strangle_strategy\data\chart_history" "$td\chart_history" /E @rc | Out-Null

# ---- CONFIG / SECRETS (private Drive only, NEVER git) ----
robocopy "$repo\config" "$cfg" settings.py symbols.py access_token.txt @rc | Out-Null
robocopy "$repo\deployment" "$cfg" .env @rc | Out-Null

$log="$dest\vps_backup_log.txt"
Add-Content $log ("{0}  OK trade-data + config synced" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
if((Test-Path $log) -and ((Get-Item $log).Length -gt 256KB)){ Set-Content $log (Get-Content $log -Tail 200) }
