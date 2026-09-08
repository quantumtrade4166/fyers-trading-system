# =============================================================================
# install_tasks.ps1 — make the month-long paper run survive reboots.
#
# Creates two Windows scheduled tasks:
#
#   DeltaBTCCollector   the option-chain archive (must never miss a day)
#   DeltaBTCEngine      the three paper strangle books
#
# Both start at boot AND immediately, restart on failure, and have no execution
# time limit — the default 3-day limit would silently kill a month-long run.
#
# WHY SCHEDULED TASKS AND NOT "just leave a terminal open"
# The first attempt at this ran the collector as a background process inside a
# tool session. It died three times in four days and left holes in the archive
# (5 Sep missing entirely, 6 and 8 Sep partial). Data that is not captured as it
# happens is gone permanently, so the runner has to outlive whatever started it.
#
# CLOCK NOTE
# This machine runs on GMT+10, not IST. That does NOT matter here: both scripts
# derive IST from UTC internally, so every session boundary is correct wherever
# they run. It only means you cannot read the task's "start time" as IST.
#
# Run from an ADMIN PowerShell, from wherever the repo lives:
#   local:  powershell -ExecutionPolicy Bypass -File `
#             G:\fyers_data_pipeline\live_trading_options\delta_btc\tools\install_tasks.ps1
#   VPS:    powershell -ExecutionPolicy Bypass -File `
#             C:\Users\Administrator\Desktop\fyers_data_pipeline_git\live_trading_options\delta_btc\tools\install_tasks.ps1
#
#   ... -Remove     to delete both tasks
#   ... -Status     to show what is installed and running
# =============================================================================
param(
    [switch]$Remove,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
# Derived from this script's own location, never hardcoded: the same file has to
# work on the local G: drive and on the VPS under
# C:\Users\Administrator\Desktop\fyers_data_pipeline_git. A hardcoded G:\ path is
# how a deploy "succeeds" and then quietly runs nothing.
$root = Split-Path -Parent $PSScriptRoot

$tasks = @(
    @{ Name = "DeltaBTCCollector"
       Bat  = "$root\run_collector.bat"
       Desc = "Delta Exchange India BTC option-chain archive (1-min snapshots). Expired contracts cannot be fetched later, so this must not miss days." },
    @{ Name = "DeltaBTCEngine"
       Bat  = "$root\run_engine.bat"
       Desc = "BTC delta-neutral strangle - three session profiles, PAPER ONLY. Resumes any in-flight cycle on restart." }
)

if ($Status) {
    foreach ($t in $tasks) {
        $task = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Host ("  {0,-20} NOT INSTALLED" -f $t.Name) -ForegroundColor Yellow
        } else {
            $info = Get-ScheduledTaskInfo -TaskName $t.Name
            Write-Host ("  {0,-20} {1,-10} last run {2}" -f $t.Name, $task.State, $info.LastRunTime)
        }
    }
    # the loopback locks are the honest answer to "is it actually running"
    foreach ($p in @(@{n="collector";port=47654}, @{n="engine";port=47655})) {
        $used = (Get-NetTCPConnection -LocalPort $p.port -State Listen -ErrorAction SilentlyContinue)
        $state = if ($used) { "HOLDING LOCK (running)" } else { "lock free (not running)" }
        Write-Host ("  {0,-20} port {1}: {2}" -f $p.n, $p.port, $state)
    }
    return
}

foreach ($t in $tasks) {
    if (Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
        Write-Host "  removed existing task $($t.Name)"
    }
}
if ($Remove) {
    Write-Host "`n  Both tasks removed. The paper run and the archive are now STOPPED." -ForegroundColor Yellow
    return
}

foreach ($t in $tasks) {
    if (-not (Test-Path $t.Bat)) { throw "missing launcher: $($t.Bat)" }

    $action = New-ScheduledTaskAction -Execute $t.Bat
    $atBoot = New-ScheduledTaskTrigger -AtStartup
    $atLogon = New-ScheduledTaskTrigger -AtLogOn

    # ExecutionTimeLimit 0 = run forever. The default is 3 days, which would have
    # killed a 30-day paper run three-quarters of the way through with no error.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $t.Name -Action $action `
        -Trigger @($atBoot, $atLogon) -Settings $settings `
        -Description $t.Desc -RunLevel Highest -Force | Out-Null

    Start-ScheduledTask -TaskName $t.Name
    Write-Host "  installed + started $($t.Name)" -ForegroundColor Green
}

Write-Host ""
Write-Host "  Both tasks are installed, start at boot, and restart on failure."
Write-Host "  Check:   powershell -File `"$root\tools\install_tasks.ps1`" -Status"
Write-Host "  Results: .venv\Scripts\python.exe live_trading_options\delta_btc\tools\compare.py"
Write-Host ""
