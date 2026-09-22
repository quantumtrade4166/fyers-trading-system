# restart_server.ps1 — restart the DASHBOARD, and only the dashboard.
#
# This used to be `taskkill /F /IM python.exe /T`: every Python process on the box.
# That is not "restart the server", it is "kill every live trading engine". On
# 2026-09-22 the dashboard was redeployed ten times during market hours and each
# one took the live Delta Neutral engine down with it, along with the VWAP engine,
# BTC, the pivot engine and DualMom. Nine DN restarts in two hours; one of them
# lost the position size and opened an 8-lot strangle's replacement leg at 1 lot.
#
#   restart_server.bat          -> dashboard only (safe at any time)
#   restart_server.bat -All     -> every python process, the OLD behaviour.
#                                  REFUSED during market hours (09:00-15:35 Mon-Fri).
#
# To restart a single engine, kill its own python and its launcher relaunches it:
#   live_trading_options\tools\restart_dn.ps1
param([switch]$All)

$now    = Get-Date                     # the VPS clock is IST
$wkday  = $now.DayOfWeek -notin 'Saturday', 'Sunday'
$market = $wkday -and $now.TimeOfDay -ge [TimeSpan]'09:00' -and $now.TimeOfDay -le [TimeSpan]'15:35'

if ($All -and $market) {
    Write-Host "REFUSED: -All kills every live trading engine and it is market hours."
    Write-Host "Restart the dashboard alone (no -All), or one engine with its own tool."
    exit 1
}

$pattern = if ($All) { '*' } else { '*uvicorn*deployment.main*' }
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
           Where-Object { $_.CommandLine -like $pattern })
$procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host ("stopped {0} python process(es) matching '{1}'" -f $procs.Count, $pattern)

Start-Sleep -Seconds 2
schtasks /Run /TN PairsDashboard | Out-Null
if ($All) { Write-Host "Server restarted via Task Scheduler (ALL python processes were stopped)." }
else      { Write-Host "Dashboard restarted via Task Scheduler - trading engines left running." }
