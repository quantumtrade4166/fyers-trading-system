# deploy_builder.ps1 -- install the Strategy Builder (stage 1: chain + analysis, NO orders) on the VPS.
# Arrives with builder_bundle.zip next to it on the Desktop:
#   scp builder_bundle.zip deploy_builder.ps1 vps:C:/Users/Administrator/Desktop/
#   ssh vps "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Administrator\Desktop\deploy_builder.ps1"
# Never touches a live engine. Restarts ONLY the dashboard, and only outside market hours.
$ErrorActionPreference = 'Stop'
$repo = 'C:\trading\fyers_data_pipeline'
$desk = 'C:\Users\Administrator\Desktop'
$py = "$repo\.venv\Scripts\python.exe"
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$bk = "$repo\_deploy_backups\builder_$stamp"

"== 1. unpack (every overwritten file is kept in $bk)"
$tmp = "$desk\builder_unpack"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive "$desk\builder_bundle.zip" $tmp
New-Item -ItemType Directory -Force $bk | Out-Null
Get-ChildItem $tmp -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($tmp.Length + 1)
    $dst = Join-Path $repo $rel
    if (Test-Path $dst) {
        $b = Join-Path $bk $rel
        New-Item -ItemType Directory -Force (Split-Path $b) | Out-Null
        Copy-Item $dst $b
    }
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Copy-Item $_.FullName $dst -Force
    "   $rel"
}

"== 2. syntax + maths tests"
Push-Location $repo
& $py -m py_compile deployment\main.py live_trading_options\strategy_builder\service.py live_trading_options\strategy_builder\chain_feed.py
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "syntax check failed -- restore from $bk" }
& $py deployment\check_dashboard_js.py 2>&1 | Select-Object -Last 1
& $py -m live_trading_options.strategy_builder.test_greeks 2>&1 | Select-Object -Last 1
Pop-Location

"== 3. scheduled task StrategyBuilder (boot + every 5 min, SYSTEM, IgnoreNew)"
$system = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$act = New-ScheduledTaskAction -Execute "$repo\live_trading_options\strategy_builder\run_builder.bat" -WorkingDirectory $repo
$boot = New-ScheduledTaskTrigger -AtStartup; $boot.Delay = 'PT30S'
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName 'StrategyBuilder' -Action $act -Trigger @($boot, $every) -Principal $system -Settings $set -Force | Out-Null
schtasks /Run /TN StrategyBuilder | Out-Null
$ok = $false
foreach ($i in 1..30) {
    Start-Sleep 2
    try { $h = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:47674/api/sb/health'; if ($h.StatusCode -eq 200) { $ok = $true; break } } catch {}
}
if ($ok) { "   builder up: $($h.Content)" } else { "   !! builder did not answer on :47674 within 60 s -- see logs\strategy_builder*.log" }
Start-Sleep 4
try { $c = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 'http://127.0.0.1:47674/api/sb/chain?index=NIFTY'; "   chain: $($c.Content.Substring(0, [Math]::Min(220, $c.Content.Length)))" } catch { "   !! chain: $_" }

"== 4. restart the DASHBOARD only (loads the /api/sb proxy + tab)"
$ist = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), 'India Standard Time')
$mkt = $ist.DayOfWeek -ne 'Saturday' -and $ist.DayOfWeek -ne 'Sunday' -and $ist.TimeOfDay -ge [TimeSpan]'09:00' -and $ist.TimeOfDay -le [TimeSpan]'15:35'
if ($mkt) {
    "   market hours ($ist) -- NOT restarting; the tab appears after the next dashboard restart"
} else {
    $procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*uvicorn deployment.main*' }
    foreach ($p in $procs) { "   stop PID $($p.ProcessId)"; Stop-Process -Id $p.ProcessId -Force }
    Start-Sleep 3
    schtasks /Run /TN PairsDashboard | Out-Null
    $ok = $false
    foreach ($i in 1..45) {
        Start-Sleep 2
        try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:8000/api/sb/health'; if ($r.StatusCode -eq 200) { $ok = $true; break } } catch {}
    }
    if ($ok) { "   dashboard -> builder proxy OK: $($r.Content.Substring(0, [Math]::Min(160, $r.Content.Length)))" } else { "   !! dashboard did not answer /api/sb/health within 90 s" }
    try { $b = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:8000/static/builder.html'; "   builder.html $($b.StatusCode) ($($b.Content.Length) bytes)" } catch { "   !! builder.html: $_" }
    try { $i = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:8000/'; "   index has tab: $($i.Content.Contains('tab-builder'))" } catch { "   !! index: $_" }
}

"== 5. clean up"
Remove-Item -Recurse -Force $tmp
Remove-Item -Force "$desk\builder_bundle.zip"
"DEPLOY DONE (backup: $bk)"
Remove-Item -Force $MyInvocation.MyCommand.Path
