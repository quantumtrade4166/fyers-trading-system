# pc_backup.ps1 -- copies EVERYTHING in the trading project and JARVIS to Google Drive.
# Run every 30 minutes by scheduled task 'PCBackupToDrive' (hidden, via pc_backup_hidden.vbs).
#
# Rule (owner, 2026-10-07): everything is backed up automatically. So this copies whole
# folders and excludes only things that are rebuildable: the Python environment (.venv),
# Git's internal folder (.git -- GitHub holds that history), caches and Claude worktrees.
#
# Copy-only (robocopy /E, no /MIR): deleting or breaking a file on this PC never removes
# the good copy from Drive. Google Drive keeps earlier versions of overwritten files.

$ErrorActionPreference = 'Continue'

# Only one backup at a time (the first full copy can take longer than 30 minutes).
$mutex = New-Object System.Threading.Mutex($false, 'Global\JarvisPcBackupToDrive')
if (-not $mutex.WaitOne(0)) { exit 0 }

$destRoot = 'D:\My Drive\PC Backup'
$status = Join-Path $destRoot 'pc_backup_status.json'
$log = Join-Path $destRoot 'pc_backup_log.txt'
New-Item -ItemType Directory -Force -Path $destRoot | Out-Null
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 512KB)) { Set-Content $log (Get-Content $log -Tail 300) -Encoding utf8 }
function Log($m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding utf8 }

$jobs = @(
    @{ Name = 'fyers_data_pipeline'; Src = 'G:\fyers_data_pipeline'
       XD = @('.venv', '.git', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'worktrees', '.tmp.driveupload') },
    @{ Name = 'jarvis'; Src = 'G:\jarvis'
       XD = @('.venv', '.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache') }
)

$results = @()
foreach ($j in $jobs) {
    $dst = Join-Path $destRoot $j.Name
    $rcArgs = @($j.Src, $dst, '/E', '/COPY:DAT', '/DCOPY:T', '/R:2', '/W:5', '/MT:8', '/NFL', '/NDL', '/NP', '/NJH', '/XJ',
                '/XF', '*.pyc', 'claude.exe', '/XD') + $j.XD
    $out = & robocopy @rcArgs
    $rc = $LASTEXITCODE
    $ok = $rc -lt 8   # robocopy: 0-7 success, 8+ failure
    $copied = ($out | Select-String -Pattern '^\s*Files\s*:' | Select-Object -First 1).Line
    if ($rc -ne 0) { Log ("{0}: rc={1} {2}" -f $j.Name, $rc, ($copied -replace '\s+', ' ')) }
    $results += @{ name = $j.Name; ok = $ok; rc = $rc }
}

$allOk = -not ($results | Where-Object { -not $_.ok })
$state = @{ last_run = (Get-Date).ToString('o'); ok = $allOk; jobs = $results }
if ($allOk) { $state.last_ok = (Get-Date).ToString('o') }
elseif (Test-Path $status) {
    try { $prev = Get-Content $status -Raw | ConvertFrom-Json; if ($prev.last_ok) { $state.last_ok = $prev.last_ok } } catch { }
}
$state | ConvertTo-Json -Depth 4 | Set-Content $status -Encoding utf8
if (-not $allOk) { Log 'BACKUP FAILED - see rc values above' }
