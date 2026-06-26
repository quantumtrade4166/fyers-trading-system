# ⚠️ DEPRECATED / NOT WIRED UP. Claude near-live sync is now handled by the repeating
#    scheduled task 'ClaudeUniverseLiveSync' (runs claude_sync_once.ps1 every 1 min),
#    which proved more reliable than this long-running watcher (which spawned duplicate
#    instances and logged inconsistently). Kept for reference only; nothing launches it.
#
# sync_claude_live.ps1  --  CONTINUOUS near-live mirror of the Claude universe.
#
# Runs forever in the background (launched at logon by scheduled task
# 'ClaudeUniverseLiveSync'). Watches the transcripts folder; the moment a file
# changes it waits a short debounce (so it never copies mid-write), then does an
# incremental copy to BOTH destinations (local G: + Google Drive). Google Drive
# then pushes the cloud copy up within seconds.
#
# RPO (max work lost on a crash) ~= a few seconds.
#
# Design notes:
#   - Detection is by polling newest LastWriteTime every few seconds. This is
#     bulletproof for an unattended daemon (FileSystemWatcher can drop events
#     under load); cost is ~1s per scan, negligible.
#   - Additive copy only (/E, never /MIR) so a transient never deletes backup
#     data. The daily backup_claude.ps1 does the /MIR reconcile incl. deletions.
#   - A forced full sweep runs every $SweepSeconds as a safety net.

$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "claude_backup_common.ps1")

$PollSeconds   = 3      # how often to check for changes
$DebounceSecs  = 6      # quiet period after last change before copying
$SweepSeconds  = 120    # forced full sync even if no change detected

$logBase = Join-Path (Get-BackupBases)[0] "ClaudeBackup"
New-Item -ItemType Directory -Force -Path $logBase | Out-Null
$Log = Join-Path $logBase "livesync_log.txt"
function Log($m) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Add-Content -Path $Log -Value $line -Encoding utf8
    if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 1MB)) {   # simple log rotation
        $tail = Get-Content $Log -Tail 500
        Set-Content $Log $tail -Encoding utf8
    }
}

function Get-NewestMtime {
    $items = Get-ChildItem -Path $script:Projects, $script:OrgDir -Recurse -File -ErrorAction SilentlyContinue
    if ($items) { ($items | Measure-Object LastWriteTime -Maximum).Maximum } else { Get-Date "2000-01-01" }
}

Log "===== live sync started (poll=${PollSeconds}s debounce=${DebounceSecs}s sweep=${SweepSeconds}s) ====="
Invoke-ClaudeSync -Mirror $false -Logger { param($m) Log $m }   # initial sync

$lastMtime    = Get-NewestMtime
$lastChangeAt = Get-Date
$lastSweepAt  = Get-Date
$pending      = $false

while ($true) {
    Start-Sleep -Seconds $PollSeconds
    $now = Get-Date
    try {
        $m = Get-NewestMtime
        if ($m -gt $lastMtime) { $lastMtime = $m; $lastChangeAt = $now; $pending = $true }

        $debounced = $pending -and (($now - $lastChangeAt).TotalSeconds -ge $DebounceSecs)
        $sweepDue  = ($now - $lastSweepAt).TotalSeconds -ge $SweepSeconds

        if ($debounced -or $sweepDue) {
            Invoke-ClaudeSync -Mirror $false -Logger { param($msg) Log $msg }
            $pending = $false
            $lastSweepAt = $now
        }
    } catch {
        Log "WARN loop error: $($_.Exception.Message)"
        Start-Sleep -Seconds 5
    }
}
