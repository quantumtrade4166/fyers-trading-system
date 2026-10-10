# vps_backup.ps1 -- backs up EVERYTHING on the VPS to Google Drive.
# Owner's hard rule (2026-10-11): whatever we do on the VPS is backed up automatically.
#
# Design: everything lives under ONE root (C:\trading) plus C:\jarvis. This script copies
# those whole folders -- it never uses a list of "important" files -- and excludes only
# rebuildable things (.venv, caches, Git internals, JARVIS's live database files, which
# JARVIS backs up itself as encrypted dumps).
#
#   Live\        copy-only mirror, refreshed every run (deleting a file on the VPS never
#                removes it from Drive)
#   Snapshots\   one dated folder per day with every file changed in the last 2 days;
#                kept 30 days
#
# Run every 10 minutes by scheduled task 'VPSBackupToDrive' (hidden, via vps_backup_hidden.vbs).
# Google Drive (G:) only exists while Administrator is logged on: close Remote Desktop with
# the X (disconnect), never "Sign out".

$ErrorActionPreference = 'Continue'

$mutex = New-Object System.Threading.Mutex($false, 'Global\VPSBackupToDrive')
if (-not $mutex.WaitOne(0)) { exit 0 }

$ops = 'C:\trading\ops'
$drive = 'G:\My Drive\VPS Backup'
$status = Join-Path $ops 'vps_backup_status.json'
$log = Join-Path $ops 'logs\vps_backup.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 512KB)) { Set-Content $log (Get-Content $log -Tail 300) -Encoding utf8 }
function Log($m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding utf8 }

function Save-Status($state) {
    $state | ConvertTo-Json -Depth 4 | Set-Content $status -Encoding utf8
    if (Test-Path $drive) { Copy-Item $status (Join-Path $drive 'vps_backup_status.json') -Force -EA SilentlyContinue }
}

$prev = $null
if (Test-Path $status) { try { $prev = Get-Content $status -Raw | ConvertFrom-Json } catch { } }

if (-not (Test-Path 'G:\My Drive')) {
    Log 'Google Drive (G:) not mounted - is Administrator logged on and Drive running?'
    Save-Status @{ last_run = (Get-Date).ToString('o'); ok = $false; error = 'drive_not_mounted'; last_ok = $prev.last_ok }
    exit 1
}

# Scheduled tasks are part of the setup: export ours as XML into the backed-up root.
$taskDir = Join-Path $ops 'tasks'
New-Item -ItemType Directory -Force -Path $taskDir | Out-Null
Get-ScheduledTask | Where-Object { $_.TaskPath -eq '\' } | ForEach-Object {
    try { Export-ScheduledTask -TaskName $_.TaskName | Set-Content (Join-Path $taskDir ($_.TaskName + '.xml')) -Encoding unicode } catch { }
}

# Copy from a Volume Shadow Copy (a frozen point-in-time view of C:), never from the live
# files: robocopy holding a file open made the engines' atomic state writes fail with
# PermissionError (seen every 10 min on TICK.json, 2026-10-10). Falls back to live files
# only if the snapshot cannot be made.
$base = 'C:'
$shadowLink = 'C:\vss_backup_view'
$shadow = $null
try {
    if (Test-Path $shadowLink) { cmd /c rmdir "$shadowLink" | Out-Null }
    $res = (Get-WmiObject -List Win32_ShadowCopy).Create('C:\', 'ClientAccessible')
    if ($res.ReturnValue -ne 0) { throw "Create returned $($res.ReturnValue)" }
    $shadow = Get-WmiObject Win32_ShadowCopy | Where-Object { $_.ID -eq $res.ShadowID }
    cmd /c mklink /d "$shadowLink" "$($shadow.DeviceObject)\" | Out-Null
    if (Test-Path "$shadowLink\trading") { $base = $shadowLink } else { throw 'shadow view not readable' }
} catch {
    Log "shadow copy unavailable ($_) - copying live files"
}

$common = @('.venv', 'venv', '.git', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache')
$jobs = @(
    @{ Name = 'trading'; Src = "$base\trading"; XD = $common },
    @{ Name = 'jarvis'; Src = "$base\jarvis"; XD = $common + @("$base\jarvis\pgdata", "$base\jarvis\data\pgdata") },
    @{ Name = 'cloudflared'; Src = "$base\Users\Administrator\.cloudflared"; XD = @() },
    @{ Name = 'cloudflared_service'; Src = "$base\Windows\System32\config\systemprofile\.cloudflared"; XD = @() }
)

$results = @()
foreach ($j in $jobs) {
    if (-not (Test-Path $j.Src)) { continue }
    $dst = Join-Path $drive ('Live\' + $j.Name)
    $rcArgs = @($j.Src, $dst, '/E', '/COPY:DAT', '/DCOPY:T', '/R:2', '/W:5', '/MT:8', '/NFL', '/NDL', '/NP', '/NJH', '/NJS', '/XJ',
                '/XF', '*.pyc', '*.tmp', '*.lock')
    if ($j.XD.Count) { $rcArgs += @('/XD') + $j.XD }
    & robocopy @rcArgs | Out-Null
    $rc = $LASTEXITCODE
    if ($rc -ge 8) { Log ("{0}: robocopy rc={1}" -f $j.Name, $rc) }
    $results += @{ name = $j.Name; ok = ($rc -lt 8); rc = $rc }
}

# Daily dated snapshot: every file changed in the last 2 days, so a bad overwrite can be undone.
$today = Get-Date -Format 'yyyy-MM-dd'
$snapRoot = Join-Path $drive 'Snapshots'
$snap = Join-Path $snapRoot $today
if (-not (Test-Path $snap)) {
    foreach ($j in $jobs) {
        if (-not (Test-Path $j.Src)) { continue }
        $rcArgs = @($j.Src, (Join-Path $snap $j.Name), '/E', '/MAXAGE:2', '/COPY:DAT', '/R:2', '/W:5', '/MT:8', '/NFL', '/NDL', '/NP', '/NJH', '/NJS', '/XJ',
                    '/XF', '*.pyc', '*.tmp', '*.lock')
        if ($j.XD.Count) { $rcArgs += @('/XD') + $j.XD }
        & robocopy @rcArgs | Out-Null
        if ($LASTEXITCODE -ge 8) { Log ("snapshot {0}: robocopy rc={1}" -f $j.Name, $LASTEXITCODE) }
    }
    Log "snapshot $today written"
    Get-ChildItem $snapRoot -Directory -EA SilentlyContinue |
        Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' -and [datetime]$_.Name -lt (Get-Date).AddDays(-30) } |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force -EA SilentlyContinue; Log "snapshot $($_.Name) expired" }
}

if (Test-Path $shadowLink) { cmd /c rmdir "$shadowLink" | Out-Null }
if ($shadow) { try { $shadow.Delete() } catch { Log "could not delete shadow copy $($shadow.ID): $_" } }

$allOk = -not ($results | Where-Object { -not $_.ok })
$state = @{ last_run = (Get-Date).ToString('o'); ok = $allOk; jobs = $results; host = $env:COMPUTERNAME }
if ($allOk) { $state.last_ok = (Get-Date).ToString('o') } elseif ($prev.last_ok) { $state.last_ok = $prev.last_ok }
Save-Status $state
if (-not $allOk) { Log 'BACKUP FAILED - see rc values above' }
