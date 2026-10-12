# deploy_stdir.ps1 -- install the Nifty ST Directional PAPER engine on the VPS.
# Arrives with stdir_bundle.zip (new files only) next to it on the Desktop:
#   scp stdir_bundle.zip deploy_stdir.ps1 vps:C:/Users/Administrator/Desktop/
#   ssh vps "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Administrator\Desktop\deploy_stdir.ps1"
# Never touches a live engine. Restarts ONLY the dashboard, and only outside market hours.
$ErrorActionPreference = 'Stop'
$repo = 'C:\trading\fyers_data_pipeline'
$desk = 'C:\Users\Administrator\Desktop'
$py = "$repo\.venv\Scripts\python.exe"
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$bk = "$repo\_deploy_backups\stdir_$stamp"

"== 1. backup the files the patch edits -> $bk"
New-Item -ItemType Directory -Force $bk | Out-Null
foreach ($f in 'deployment\main.py', 'deployment\scheduler.py', 'deployment\static\index.html', 'tools\vps\install_vps_tasks.ps1') {
    $dst = Join-Path $bk $f
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Copy-Item "$repo\$f" $dst
}

"== 2. unpack new files"
$tmp = "$desk\stdir_unpack"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive "$desk\stdir_bundle.zip" $tmp
Get-ChildItem $tmp -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($tmp.Length + 1)
    $dst = Join-Path $repo $rel
    if (Test-Path $dst) {
        $b = Join-Path "$bk\overwritten" $rel
        New-Item -ItemType Directory -Force (Split-Path $b) | Out-Null
        Copy-Item $dst $b
    }
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Copy-Item $_.FullName $dst -Force
    "   $rel"
}
New-Item -ItemType Directory -Force "$repo\live_trading_options\nifty_st_directional\data\live_state", "$repo\live_trading_options\nifty_st_directional\data\results" | Out-Null

"== 3. patch dashboard (main.py router, scheduler watchdog, index.html tab, task installer)"
& $py "$repo\tools\vps\patch_stdir.py"
if ($LASTEXITCODE -ne 0) { throw "patch failed" }
& $py -m py_compile "$repo\deployment\main.py" "$repo\deployment\scheduler.py" "$repo\deployment\stdir_api.py"
if ($LASTEXITCODE -ne 0) { throw "syntax check failed -- restore from $bk" }
"   syntax ok"

"== 4. tests on the VPS"
Push-Location $repo
& $py -m pytest backtesting/options_st_directional/test_strategy.py -q -p no:cacheprovider 2>&1 | Select-Object -Last 2
& $py tools\vps\smoke_stdir.py --date 2026-10-12 2>&1 | Select-Object -Last 5
Pop-Location

"== 5. scheduled task NiftyStDirEngine (Mon-Fri 09:10, SYSTEM, no time limit)"
$weekdays = 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'
$system = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
$act = New-ScheduledTaskAction -Execute "$repo\live_trading_options\nifty_st_directional\run_stdir_engine.bat" -WorkingDirectory $repo
$trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At '09:10'
Register-ScheduledTask -TaskName 'NiftyStDirEngine' -Action $act -Trigger $trg -Principal $system -Settings $set -Force | Out-Null
Get-ScheduledTask NiftyStDirEngine | Select-Object TaskName, State | Format-Table -AutoSize | Out-String

"== 6. restart the DASHBOARD only (loads /api/stdir + watchdog)"
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
    foreach ($i in 1..30) {
        Start-Sleep 2
        try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:8000/api/stdir/dates'; if ($r.StatusCode -eq 200) { $ok = $true; break } } catch {}
    }
    if ($ok) { "   dashboard up: /api/stdir/dates -> $($r.Content)" } else { "   !! dashboard did not answer /api/stdir/dates within 60 s" }
    try { $h = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:8000/static/stdir.html'; "   stdir.html $($h.StatusCode) ($($h.Content.Length) bytes)" } catch { "   !! stdir.html: $_" }
    try { $i = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:8000/'; "   index has tab: $($i.Content.Contains('tab-stdir'))" } catch { "   !! index: $_" }
}

"== 7. clean up"
Remove-Item -Recurse -Force $tmp
Remove-Item -Force "$desk\stdir_bundle.zip"
"DEPLOY DONE (backup: $bk)"
Remove-Item -Force $MyInvocation.MyCommand.Path
