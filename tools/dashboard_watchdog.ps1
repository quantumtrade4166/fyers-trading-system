# tools/dashboard_watchdog.ps1 -- self-heal for the PairsDashboard (VPS).
# ---------------------------------------------------------------------------
# Runs every 1 min via the DashboardWatchdog scheduled task. Does ONE health check of
# http://localhost:8000 and, only after MaxFails CONSECUTIVE failures, restarts the
# dashboard -- so a normal ~30s boot is never interrupted (a fresh boot is back up by the
# next check). A cooldown stops restart-spam if a restart doesn't fix it. Single-shot by
# design: no long-running loop process to itself die.
#
# It restarts by calling restart_server.bat (kill stale python + run the PairsDashboard
# task), which now launches uvicorn with --ws wsproto, so a restart actually recovers.
# NOTE: ASCII only -- PowerShell 5.1 mis-parses non-ASCII bytes in this file.
$ErrorActionPreference = 'SilentlyContinue'

$Url         = 'http://localhost:8000/api/version'
$Restarter   = 'C:\Users\Administrator\Desktop\restart_server.bat'
$LogDir      = 'C:\Users\Administrator\Desktop\fyers_data_pipeline_git\logs'
$Log         = Join-Path $LogDir 'watchdog.log'
$FailFile    = Join-Path $LogDir 'watchdog_fails.txt'
$LastRestart = Join-Path $LogDir 'watchdog_last_restart.txt'
$MaxFails    = 2       # consecutive down-checks before restarting (~2 min of real downtime)
$CooldownS   = 300     # min seconds between watchdog-initiated restarts

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Add-Content -Path $Log -Encoding utf8 }

# health check (a hung event loop times out -> counts as down, as it should)
$ok = $false
try { $ok = ((Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 12).StatusCode -eq 200) } catch { $ok = $false }

if ($ok) {
    if ((Test-Path $FailFile) -and ((Get-Content $FailFile) -ne '0')) { Log 'recovered (200 OK)' }
    Set-Content -Path $FailFile -Value '0' -Encoding ascii
    exit 0
}

# down: bump the consecutive-failure counter
$fails = 0
if (Test-Path $FailFile) { [int]::TryParse((Get-Content $FailFile), [ref]$fails) | Out-Null }
$fails++
Set-Content -Path $FailFile -Value "$fails" -Encoding ascii
Log "DOWN (consecutive=$fails)"

if ($fails -lt $MaxFails) { exit 0 }        # one more minute -- covers a normal restart's boot

# cooldown so a bad restart can't spam
if (Test-Path $LastRestart) {
    if (((Get-Date) - (Get-Item $LastRestart).LastWriteTime).TotalSeconds -lt $CooldownS) {
        Log "in cooldown (<$CooldownS s since last restart) -- not restarting again yet"
        exit 0
    }
}

Log "RESTARTING dashboard (down $fails consecutive checks)"
& $Restarter | Out-Null
Set-Content -Path $LastRestart -Value (Get-Date -Format o) -Encoding ascii
Set-Content -Path $FailFile -Value '0' -Encoding ascii
Log 'restart triggered'
exit 0
