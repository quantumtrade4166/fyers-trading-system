# restart_dn.ps1 — restart (or stop) ONLY the Delta Neutral engine, correctly.
#
#   restart_dn.ps1          restart: the launcher relaunches it in ~5s
#   restart_dn.ps1 -Stop    stop for good (until the next scheduled 09:20 start)
#
# Why a script: `schtasks /End /TN DeltaNeutralEngine` does NOT do this. The task
# runs a .bat; End kills that cmd.exe but not the python child (the venv's
# python.exe is a redirector stub that spawns the real interpreter). On 2026-09-22
# an End + Run left NO engine at all for 2.5 minutes while a live position was open.
#
# A restart is safe at any time, positions included: the engine rebuilds what it
# holds from the broker and the day's decisions from its own snapshot.
param([switch]$Stop)

$task = 'DeltaNeutralEngine'
$dn = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*delta_neutral*engine.py*' })

if ($Stop) {
    schtasks /End /TN $task 2>&1 | Out-Null           # the launcher first, so it cannot relaunch
    Start-Sleep -Seconds 1
    $dn | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host ("DN stopped ({0} python process(es) ended). Exchange stops still protect any open legs." -f $dn.Count)
    exit 0
}

$state = (Get-ScheduledTask -TaskName $task).State
$dn | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
if ($state -ne 'Running') {
    Start-Sleep -Seconds 1
    schtasks /Run /TN $task | Out-Null                 # no launcher alive -> start one
}
Write-Host ("DN restart: ended {0} process(es); launcher was {1}. Check logs\dn_engine.log." -f $dn.Count, $state)
