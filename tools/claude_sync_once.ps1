# claude_sync_once.ps1  --  One incremental sync pass of the Claude universe to both
# destinations (local G: + Google Drive). Additive (/E, never deletes).
# Run every 1 minute by scheduled task 'ClaudeUniverseLiveSync' (near-live, RPO ~1 min).
#
# This replaces the old long-running watcher (sync_claude_live.ps1), which proved
# unreliable/opaque unattended. A repeating scheduled task is deterministic and
# restart-proof (Task Scheduler handles restarts; no duplicate-process churn).

$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "claude_backup_common.ps1")

$logBase = @(Get-BackupBases)[0]
$logDir = Join-Path $logBase "ClaudeBackup"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$Log = Join-Path $logDir "livesync_log.txt"
if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 512KB)) {
    Set-Content $Log (Get-Content $Log -Tail 300) -Encoding utf8
}
function Log($m) {
    Add-Content -Path $Log -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) -Encoding utf8
}

# Only log changes/errors (skip the every-minute "rc=0" no-change noise).
Invoke-ClaudeSync -Mirror $false -Logger { param($m) if ($m -notmatch 'rc=0/0') { Log $m } }
