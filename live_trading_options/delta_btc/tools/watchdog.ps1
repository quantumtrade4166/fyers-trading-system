# BTC engine watchdog — runs every 5 minutes from a scheduled task.
#
# The engine has its own "no good poll for 180s -> exit for a clean restart", but
# on 2026-09-16 it HUNG instead: TICK.json froze at 14:46 and the process sat
# there holding the single-instance lock, so the keep-alive task could not start a
# replacement. Nothing traded for three hours. This kills it from outside when its
# heartbeat file goes stale, which is the one failure its own watchdog cannot see.
$d    = "C:\Users\Administrator\Desktop\fyers_data_pipeline_git\live_trading_options\delta_btc"
$tick = "$d\data\live_state\TICK.json"
$log  = "$d\logs\watchdog.log"
$stale = 6      # minutes

function Note($m) { "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))  $m" | Add-Content $log }

$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
           Where-Object { $_.CommandLine -like '*delta_btc*engine.py*' -and $_.CommandLine -notlike '*vwap*' })
$age = if (Test-Path $tick) { ((Get-Date) - (Get-Item $tick).LastWriteTime).TotalMinutes } else { 999 }

if ($procs.Count -eq 0) {
    Note "no engine running - starting"
    Start-ScheduledTask -TaskName DeltaBTCEngine
} elseif ($age -gt $stale) {
    Note ("tick is {0:N1} min stale - killing {1} process(es) and restarting" -f $age, $procs.Count)
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 5
    Start-ScheduledTask -TaskName DeltaBTCEngine
}
