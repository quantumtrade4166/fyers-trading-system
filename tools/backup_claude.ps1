# backup_claude.ps1  --  DAILY FULL RECONCILE of the Claude universe.
#
# Mirrors (/MIR) the ~130 MB of irreplaceable data to BOTH destinations:
#   - local  : G:\ClaudeBackups\ClaudeBackup
#   - cloud  : H:\My Drive\ClaudeBackup  (Google Drive -> syncs to cloud automatically)
#
# Backs up: ~/.claude/projects (transcripts = source of truth), ~/.claude configs,
#           Desktop sidebar index (local_*.json), claude_desktop_config.json.
# Excludes: 9.5 GB app cache/leveldb, and the machine-bound OAuth token (config.json).
#
# This is the "full reconcile" pass -- /MIR also propagates DELETIONS so the
# backups stay exact mirrors. Continuous near-live copying is handled separately
# by sync_claude_live.ps1. Run by scheduled task 'ClaudeUniverseBackup' daily 21:30.

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "claude_backup_common.ps1")

$logBase = Join-Path (Get-BackupBases)[0] "ClaudeBackup"
New-Item -ItemType Directory -Force -Path $logBase | Out-Null
$Log = Join-Path $logBase "backup_log.txt"
function Log($m) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Write-Output $line
    Add-Content -Path $Log -Value $line -Encoding utf8
}

Log "===== daily reconcile start ====="
Invoke-ClaudeSync -Mirror $true -Logger { param($m) Log $m }

# Write/refresh the restore guide into every destination
$readme = @"
HOW TO RESTORE THE CLAUDE UNIVERSE
==================================
Backup made by backup_claude.ps1 (daily) + sync_claude_live.ps1 (continuous).

WHAT'S HERE
  claude\         -> full copy of C:\Users\<you>\.claude
                     (claude\projects = ALL transcripts = source of truth)
  desktop-index\  -> Claude Desktop sidebar index files (local_*.json)
  desktop-config\ -> claude_desktop_config.json (MCP servers)
  home\           -> ~/.claude.json, ~/.claude.json.backup, ~/.mcp.json
                     (global CLI state: MCP servers, project trust, history)

RESTORE ON A NEW / WIPED MACHINE
  1. Install Claude Code (CLI) + Claude Desktop, sign in once.
  2. Copy 'claude\' back to  C:\Users\<you>\.claude  (at minimum claude\projects),
     and copy 'home\' files back to  C:\Users\<you>\  (e.g. ~/.claude.json).
  3. Rebuild the Desktop sidebar from the transcripts:
         python G:\fyers_data_pipeline\tools\rebuild_desktop_index.py --write
     (override --account/--org/--projects if the new machine's GUIDs differ; find
      them under <AppData>\Claude\claude-code-sessions\)
  4. Fully quit + relaunch Claude Desktop.
NOTE: The OAuth token is intentionally NOT backed up (machine-bound). Just sign in.
"@
foreach ($base in (Get-BackupBases)) {
    Set-Content -Path (Join-Path (Join-Path $base "ClaudeBackup") "RESTORE_README.txt") -Value $readme -Encoding utf8
}
Log "===== daily reconcile done ====="
