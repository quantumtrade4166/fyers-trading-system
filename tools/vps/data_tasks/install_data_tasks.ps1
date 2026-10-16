# install_data_tasks.ps1 -- daily market-data downloads on the VPS (2026-10-12).
# The 10-Oct rebuild COPIED these datasets from Drive once; nothing kept them current.
# Idempotent (-Force). Runs as SYSTEM, IST clock. Called by tools\vps\install_vps_tasks.ps1.
#   powershell -ExecutionPolicy Bypass -File C:\trading\fyers_data_pipeline\tools\vps\data_tasks\install_data_tasks.ps1 [-RunNow]
param([switch]$RunNow)
$ErrorActionPreference = 'Stop'
$repo = 'C:\trading\fyers_data_pipeline'
$dir = "$repo\tools\vps\data_tasks"
$weekdays = 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'
$system = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

function Add-DataTask($name, $bat, $trigger, $limitMin) {
    $set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes $limitMin)
    $act = New-ScheduledTaskAction -Execute "$dir\$bat" -WorkingDirectory $repo
    Register-ScheduledTask -TaskName $name -Action $act -Trigger $trigger -Principal $system -Settings $set -Force | Out-Null
    "task: $name  ($bat)"
}
# After the 15:30 close, before the evening; forex at night (after the NIFTY engines, never in market hours)
Add-DataTask 'DataNiftyStocks5m'  'dl_nifty_stocks.bat'  (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At '15:45') 60
Add-DataTask 'DataNiftyOptions1m' 'dl_nifty_options.bat' (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At '15:55') 90
Add-DataTask 'DataBhavcopy'       'dl_bhavcopy.bat'      (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At '19:30') 30
Add-DataTask 'DataStDirBacktest'  'dl_stdir_backtest.bat' (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At '16:30') 60
Add-DataTask 'DataDukascopy1m'    'dl_dukascopy_1m.bat'  (New-ScheduledTaskTrigger -Daily -At '21:00') 180
Add-DataTask 'DataXauusd1s'       'dl_xauusd_1s.bat'     (New-ScheduledTaskTrigger -Daily -At '22:30') 240

# Dukascopy needs Node.js + forex\node_modules (the restore skips node_modules on purpose)
$node = Get-Command node -EA SilentlyContinue
if ($node) {
    "node $(& node --version) at $($node.Source)"
    Push-Location "$repo\forex"
    if (-not (Test-Path 'node_modules\dukascopy-node')) { & npm ci --no-audit --no-fund 2>&1 | Select-Object -Last 3 }
    Pop-Location
    "dukascopy-node installed: $(Test-Path "$repo\forex\node_modules\dukascopy-node")"
} else {
    "!! Node.js is NOT installed -- DataDukascopy1m / DataXauusd1s will fail until it is (then run: cd forex; npm ci)"
}

if ($RunNow) {
    foreach ($t in 'DataNiftyStocks5m', 'DataNiftyOptions1m', 'DataBhavcopy') { Start-ScheduledTask $t; "started now: $t" }
    if ($node) { Start-ScheduledTask 'DataDukascopy1m'; "started now: DataDukascopy1m" }
}
