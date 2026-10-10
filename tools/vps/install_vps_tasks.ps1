# install_vps_tasks.ps1 -- creates EVERY scheduled task the VPS needs, from one list.
# Run on the VPS as Administrator after the code + .venv are in C:\trading\fyers_data_pipeline:
#     powershell -ExecutionPolicy Bypass -File C:\trading\fyers_data_pipeline\tools\vps\install_vps_tasks.ps1
# Idempotent (-Force). Rebuilt 2026-10-11 after PowerHost reinstalled the VPS; the old
# per-task XML exports carried the old machine's user SID and Desktop paths.
#
# Not here: VPSBackupToDrive (tools/vps/vps_step2_backup.ps1 -- installed FIRST),
# CloudflaredTunnel (needs the tunnel token), JARVIS (its own installer), and the BTC VWAP
# engine (runs on the home PC since 2026-10-10 -- moving it back is a deliberate decision).
param([switch]$EnableDualMom)

$ErrorActionPreference = 'Stop'
$repo = 'C:\trading\fyers_data_pipeline'
$weekdays = 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'
$system = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

function Settings($limitMinutes, $restart) {
    $p = @{ MultipleInstances = 'IgnoreNew'; StartWhenAvailable = $true; AllowStartIfOnBatteries = $true; DontStopIfGoingOnBatteries = $true }
    $p.ExecutionTimeLimit = if ($limitMinutes) { New-TimeSpan -Minutes $limitMinutes } else { [TimeSpan]::Zero }
    if ($restart) { $p.RestartCount = 999; $p.RestartInterval = New-TimeSpan -Minutes 1 }
    New-ScheduledTaskSettingsSet @p
}
function Daily($hhmm) { New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $hhmm }
function Every($minutes) { New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $minutes) }
function Boot { $t = New-ScheduledTaskTrigger -AtStartup; $t.Delay = 'PT30S'; $t }

function Add-Task($name, $exe, $arg, $triggers, $limitMinutes = 0, [switch]$Restart) {
    $a = if ($arg) { New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $repo } else { New-ScheduledTaskAction -Execute $exe -WorkingDirectory $repo }
    Register-ScheduledTask -TaskName $name -Action $a -Trigger $triggers -Principal $system -Settings (Settings $limitMinutes $Restart) -Force | Out-Null
    "task: $name"
}
$ps = 'powershell.exe'
$psArgs = { param($f) "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$f`"" }

# ---- always on --------------------------------------------------------------------------
Add-Task 'PairsDashboard'      "$repo\deployment\vps_tasks\start_dashboard.bat" $null @(Boot) -Restart
Add-Task 'VPSHealthHeartbeat'  $ps (& $psArgs "$repo\deployment\vps_tasks\vps_heartbeat.ps1") @(Every 5) 4
Add-Task 'DualMomService'      "$repo\start_dualmom_service.bat" $null @((Boot), (Every 5)) -Restart
if (-not $EnableDualMom) {
    # Live money (Kotak + Kite). Stays OFF until the owner has whitelisted the new VPS IP at
    # both brokers -- otherwise it would retry failing logins every 5 minutes.
    Disable-ScheduledTask -TaskName 'DualMomService' | Out-Null
    "   DualMomService DISABLED (run again with -EnableDualMom once the brokers know the new IP)"
}

# ---- market-day chain (IST, Mon-Fri) ------------------------------------------------------
Add-Task 'FyersAutoLogin'      "$repo\deployment\vps_tasks\fyers_auto_login.bat" $null @(Daily '09:00') 10
Add-Task 'ZerodhaAutoLogin'    "$repo\deployment\vps_tasks\zerodha_auto_login.bat" $null @(Daily '09:00') 10
Add-Task 'StrangleMorningFlag' "$repo\strangle_system\deploy\run_decision_flag_vps.bat" $null @(Daily '09:10') 30
Add-Task 'NiftyPivotEngine'    "$repo\live_trading_options\nifty_pivot\run_pivot_engine.bat" $null @(Daily '09:10')
Add-Task 'DeltaNeutralPreflight' "$repo\live_trading_options\delta_neutral\run_dn_preflight.bat" $null @(Daily '09:15') 10
Add-Task 'DeltaNeutralEngine'  "$repo\live_trading_options\delta_neutral\run_dn_engine.bat" $null @(Daily '09:20')
Add-Task 'StrangleV2Engine'    "$repo\live_trading_options\strangle_strategy\deploy\run_v2_engine_vps.bat" $null @(Daily '09:20')
foreach ($t in '15:05', '15:15', '15:25') {
    Add-Task ('StrangleChainCapture_' + $t.Replace(':', '')) "$repo\strangle_system\deploy\run_chain_collector_vps.bat" $null @(Daily $t) 9
}
Add-Task 'SquareOffWatchdog'   "$repo\live_trading_options\tools\run_squareoff_watchdog.bat" $null @(Daily '15:20') 60
Add-Task 'StrangleChartArchive' "$repo\live_trading_options\strangle_strategy\deploy\run_daily_archive_vps.bat" $null @(Daily '15:31') 30

# ---- per-strategy installers (they own their task definitions) ------------------------------
& "$repo\tools\setup_watchdog_task.ps1"                                   # DashboardWatchdog
& "$repo\live_trading_options\delta_btc\tools\install_tasks.ps1"          # DeltaBTCCollector + DeltaBTCEngine (paper)
& "$repo\live_trading_options\delta_btc\tools\install_watchdog.ps1"       # DeltaBTCWatchdog
& "$repo\live_trading_options\delta_btc\tools\install_recorder_task.ps1"  # LiquidityRecorder
& "$repo\crypto\btc_arbitrage\tools\install_task.ps1"                     # BtcArbMonitor

''
Get-ScheduledTask | Where-Object { $_.TaskPath -eq '\' } | Sort-Object TaskName |
    ForEach-Object { '{0,-26} {1}' -f $_.TaskName, $_.State }
