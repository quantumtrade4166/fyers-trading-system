# ⚠️ DEPRECATED / NOT WIRED UP. Vault near-live sync is handled by the repeating
#    scheduled task 'ObsidianVaultLiveSync' (runs vault_backup.ps1 every 2 min),
#    which proved more reliable unattended than a long-running watcher. Kept for
#    reference only; nothing launches this script.
#
# vault_sync_live.ps1  --  CONTINUOUS near-live mirror of the Obsidian "Trading Brain"
# vault to Google Drive. Runs forever in the background (launched at logon by the
# Startup-folder launcher). Watches the vault; on any change it debounces a few
# seconds then does an additive copy. Google Drive then uploads within seconds.
# RPO (max work lost on a crash) ~= a few seconds.

$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "vault_common.ps1")

$PollSeconds  = 3
$DebounceSecs = 6
$SweepSeconds = 120

$dest = Get-VaultDest
$logDir = if ($dest) { $dest } else { "G:\ClaudeBackups" }
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$Log = Join-Path $logDir "vault_livesync_log.txt"
function Log($m) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Add-Content -Path $Log -Value $line -Encoding utf8
    if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 1MB)) {
        Set-Content $Log (Get-Content $Log -Tail 500) -Encoding utf8
    }
}

function Get-NewestMtime {
    $items = Get-ChildItem -Path $script:VaultSrc -Recurse -File -EA SilentlyContinue |
             Where-Object { $_.Name -notin $script:VaultExcludeFiles }
    if ($items) { ($items | Measure-Object LastWriteTime -Maximum).Maximum } else { Get-Date "2000-01-01" }
}

Log "===== vault live sync started (poll=${PollSeconds}s debounce=${DebounceSecs}s sweep=${SweepSeconds}s) ====="
Invoke-VaultSync -Mirror $false -Logger { param($m) Log $m }

$lastMtime = Get-NewestMtime
$lastChangeAt = Get-Date
$lastSweepAt = Get-Date
$pending = $false

while ($true) {
    Start-Sleep -Seconds $PollSeconds
    $now = Get-Date
    try {
        $m = Get-NewestMtime
        if ($m -gt $lastMtime) { $lastMtime = $m; $lastChangeAt = $now; $pending = $true }
        $debounced = $pending -and (($now - $lastChangeAt).TotalSeconds -ge $DebounceSecs)
        $sweepDue  = ($now - $lastSweepAt).TotalSeconds -ge $SweepSeconds
        if ($debounced -or $sweepDue) {
            Invoke-VaultSync -Mirror $false -Logger { param($msg) Log $msg }
            $pending = $false
            $lastSweepAt = $now
        }
    } catch {
        Log "WARN loop error: $($_.Exception.Message)"
        Start-Sleep -Seconds 5
    }
}
