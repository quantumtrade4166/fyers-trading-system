# =============================================================================
# install_recorder_task.ps1 — scheduled task for the Binance vs Delta liquidity recorder.
#
#   LiquidityRecorder   Binance vs Delta BTC options (tools\record_binance_delta.py)
#
# Same shape as install_vwap_task.ps1: starts at boot/logon, plus a 5-minute
# keep-alive trigger (IgnoreNew makes it a no-op while running) because
# RestartCount does not revive a process killed by `taskkill /IM python.exe`.
#
#   powershell -ExecutionPolicy Bypass -File tools\install_recorder_task.ps1
#   ... -Remove    delete the task
#   ... -Status    show task + lock port 47662
# =============================================================================
param([switch]$Remove, [switch]$Status)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$name = "LiquidityRecorder"
$bat = "$root\run_liquidity_recorder.bat"

if ($Status) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $t) { Write-Host "  $name NOT INSTALLED" }
    else { Write-Host ("  {0} {1} last run {2}" -f $name, $t.State, (Get-ScheduledTaskInfo -TaskName $name).LastRunTime) }
    $used = Get-NetTCPConnection -LocalPort 47662 -State Listen -ErrorAction SilentlyContinue
    Write-Host ("  port 47662: " + $(if ($used) { "HOLDING LOCK (running)" } else { "lock free (not running)" }))
    return
}

if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "  removed existing $name"
}
if ($Remove) { return }
if (-not (Test-Path $bat)) { throw "missing launcher: $bat" }

$action = New-ScheduledTaskAction -Execute $bat
$keepAlive = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action `
    -Trigger @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn), $keepAlive) `
    -Settings $settings -Description "Binance vs Delta BTC options liquidity recorder. Read-only public data." `
    -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "  installed + started $name" -ForegroundColor Green
