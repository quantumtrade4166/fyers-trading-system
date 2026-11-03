# install_vps.ps1 -- CE/PE Imbalance PAPER engine on the VPS: scheduled task only (no tunnel; the
# dashboard proxies /api/cepe/* to 127.0.0.1:47690). Run as Administrator:
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\trading\fyers_data_pipeline\live_trading_options\cepe_imbalance\install_vps.ps1
# Task "CepeImbalance" (SYSTEM): boot + every 5 min, one instance (port 47690 = single-instance lock).
$ErrorActionPreference = 'Continue'
$repo = 'C:\trading\fyers_data_pipeline'
$bat = "$repo\live_trading_options\cepe_imbalance\run_cepe.bat"
$a = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $repo
$boot = New-ScheduledTaskTrigger -AtStartup; $boot.Delay = 'PT50S'
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
     -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'CepeImbalance' -Action $a -Trigger @($boot, $every) -Principal $p -Settings $s -Force | Out-Null
'task: CepeImbalance registered'
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*cepe_imbalance.service*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "stopped hand-started engine PID $($_.ProcessId)" }
Start-Sleep 2
Start-ScheduledTask -TaskName 'CepeImbalance'
$ok = $false
foreach ($i in 1..40) {
    Start-Sleep 2
    try { $h = Invoke-RestMethod -Uri 'http://127.0.0.1:47690/health' -TimeoutSec 3; if ($h.ok) { $ok = $true; break } } catch {}
}
if ($ok) { "engine up: feed authed, subscribed $($h.feed.subscribed), universe $($h.universe_date)" } else { 'WARNING: engine not healthy after 80 s - see logs\cepe_imbalance.log' }
