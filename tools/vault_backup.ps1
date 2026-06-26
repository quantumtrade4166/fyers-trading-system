# vault_backup.ps1  --  FULL /MIR sync of the Obsidian "Trading Brain" vault to
# Google Drive (My Drive\Obsidian Vault backup). /MIR also propagates deletions.
# Run every 2 minutes by scheduled task 'ObsidianVaultLiveSync' (near-live, RPO ~2 min)
# -- a repeating scheduled task is used instead of a long-running watcher because it
# is more robust unattended (Task Scheduler handles restarts; no crash-loop risk).

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "vault_common.ps1")

$dest = Get-VaultDest
$logDir = if ($dest) { $dest } else { "G:\ClaudeBackups" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$Log = Join-Path $logDir "vault_backup_log.txt"
# rotate: keep the log from growing unbounded (runs every 2 min)
if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 512KB)) {
    Set-Content $Log (Get-Content $Log -Tail 300) -Encoding utf8
}
function Log($m) {
    Add-Content -Path $Log -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) -Encoding utf8
}

# Only log when something actually changed or errored (skip the every-2-min "rc=0" noise)
Invoke-VaultSync -Mirror $true -Logger { param($m) if ($m -notmatch 'rc=0(\b|$)') { Log $m; Write-Output $m } }
