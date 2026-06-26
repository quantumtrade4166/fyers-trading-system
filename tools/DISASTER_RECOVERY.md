# 🚑 Claude Universe — Disaster Recovery Runbook

**Purpose:** Fully restore Claude Code sessions (transcripts + sidebar names + config)
after a machine crash, disk failure, OS reinstall, or migration to a new machine.

There are **two copies of everything**, so recovery works even if one is gone:
- **Google Drive** (cloud): `My Drive/ClaudeBackup/` — survives total disk loss.
- **GitHub** (the tooling/scripts): `https://github.com/quantumtrade4166/fyers-trading-system` → `tools/`
- (Local secondary copy `G:\ClaudeBackups\ClaudeBackup` — only if that disk survived.)

The backup also ships a self-contained copy of these scripts at
`My Drive/ClaudeBackup/recovery-kit/`, so you can recover with **only** Google Drive.

---

## PART 1 — What YOU do (human steps, ~15 min)

1. **Get a working Windows machine** (new or wiped).
2. **Install Google Drive for Desktop** and sign in. Wait for it to mount
   (e.g. `H:\My Drive`). Confirm you can see `My Drive\ClaudeBackup\`.
3. **Install Claude Code (CLI) and Claude Desktop**, then **sign in to Claude**
   in both. (Your old login token is intentionally NOT restored — just sign in again.)
4. **Open a NEW Claude Code session** in any folder and paste the prompt in **Part 2**.
   Claude will do the rest (restore files, rebuild the sidebar, re-arm the backups).
5. **Fully quit and relaunch Claude Desktop.** Your sessions reappear in the sidebar
   with their names.

> Prefer to do it by hand instead of via Claude? Follow **Part 3** yourself.

---

## PART 2 — The prompt to paste into the fresh Claude Code session

Copy everything between the lines:

```
You are recovering my Claude environment after a machine crash. Read and follow the
runbook at  <Google Drive>\ClaudeBackup\recovery-kit\DISASTER_RECOVERY.md
(if you can't find it, it's also at the GitHub repo
https://github.com/quantumtrade4166/fyers-trading-system in tools/DISASTER_RECOVERY.md).

Locate my backup folder (search for "ClaudeBackup" under the Google Drive mount, e.g.
H:\My Drive\ClaudeBackup). Then execute PART 3 of that runbook end to end:
 1. Restore ~/.claude (especially projects/) and the home/ config files.
 2. Rebuild the Claude Desktop sidebar index with rebuild_desktop_index.py --write.
 3. Re-arm the live-sync watcher (Startup VBS) and the daily backup scheduled task.
 4. Verify everything and report what was restored (session count, etc.).
Tell me to fully restart Claude Desktop when done. Confirm each major step as you go.
```

---

## PART 3 — Detailed restore steps (Claude executes these)

**Paths assume Windows. `$BK` = the backup root, e.g. `H:\My Drive\ClaudeBackup`.**
Find it first: search the Google Drive mount for a folder named `ClaudeBackup`.

### Step 1 — Restore the transcripts + CLI config (the source of truth)
```powershell
$BK = "H:\My Drive\ClaudeBackup"          # adjust drive letter if different
# transcripts + ~/.claude config
robocopy "$BK\claude" "$env:USERPROFILE\.claude" /E /R:1 /W:1
# home-dir CLI config files (global state, MCP servers, project trust)
Copy-Item "$BK\home\.claude.json"        "$env:USERPROFILE\.claude.json"        -Force -EA SilentlyContinue
Copy-Item "$BK\home\.claude.json.backup" "$env:USERPROFILE\.claude.json.backup" -Force -EA SilentlyContinue
Copy-Item "$BK\home\.mcp.json"           "$env:USERPROFILE\.mcp.json"           -Force -EA SilentlyContinue
# Desktop MCP config (optional)
# Copy-Item "$BK\desktop-config\claude_desktop_config.json" <Claude AppData>\claude_desktop_config.json -Force
```

### Step 2 — Rebuild the Desktop sidebar index
The sidebar index files are NOT auto-recreated; regenerate them from the transcripts.
Use the script from the recovery kit (or clone the GitHub repo to get it):
```powershell
# from the recovery kit shipped in the backup:
python "$BK\recovery-kit\rebuild_desktop_index.py" --write
```
**If the account/org GUID differs on the new machine** (rare — they follow your
Anthropic account, not the machine), find the new ones by listing
`<Claude AppData>\claude-code-sessions\<accountId>\<orgId>\` and pass them:
```powershell
python rebuild_desktop_index.py --write --account <ACCT-GUID> --org <ORG-GUID> `
    --projects "$env:USERPROFILE\.claude\projects" --appdata "<Claude AppData root>"
```
> The old sidebar **names** are also restored automatically, because they live in the
> backed-up `desktop-index\...\local_*.json` files. If you'd rather copy those names
> straight back (only valid when account/org GUIDs match), copy
> `$BK\desktop-index\<acct>\<org>\local_*.json` into the live app's
> `claude-code-sessions\<acct>\<org>\` folder instead of regenerating.

### Step 3 — Re-arm the automatic backups on the new machine
The live watcher + daily job lived on the old machine — set them up again:
```powershell
$tools = "$BK\recovery-kit"   # or your cloned repo's tools\ folder
# Live watcher at logon (per-user Startup launcher, no admin needed):
$startup = [Environment]::GetFolderPath('Startup')
$vbs = Join-Path $startup "ClaudeLiveSync.vbs"
@'
Set s = CreateObject("WScript.Shell")
s.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""<TOOLS>\sync_claude_live.ps1""", 0, False
'@.Replace("<TOOLS>", $tools) | Set-Content $vbs -Encoding ascii
Start-Process wscript.exe -ArgumentList "`"$vbs`"" -WindowStyle Hidden   # start now
# Daily full reconcile at 21:30:
$act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$tools\backup_claude.ps1`""
$trg = New-ScheduledTaskTrigger -Daily -At 9:30PM
Register-ScheduledTask -TaskName "ClaudeUniverseBackup" -Action $act -Trigger $trg -Force
```
> Best practice: clone the GitHub repo and point the Startup/daily task at its real
> `tools\` folder (kept up to date) rather than the recovery-kit snapshot.

### Step 4 — Verify
```powershell
(Get-ChildItem "$env:USERPROFILE\.claude\projects" -Recurse -Filter *.jsonl).Count   # transcripts restored
(Get-ChildItem "<Claude AppData>\claude-code-sessions\<acct>\<org>\local_*.json").Count  # index files
```
Then **fully quit + relaunch Claude Desktop** → sessions appear in the sidebar.

---

## Restoring the Obsidian vault ("Trading Brain")

The vault is backed up separately (Google Drive only) at
`<Google Drive>\My Drive\Obsidian Vault backup\`, synced every 2 minutes by the
scheduled task `ObsidianVaultLiveSync` (which runs `vault_backup.ps1`).

To restore it on a new machine:
```powershell
# 1. copy the vault back to its location (adjust drive letter)
robocopy "H:\My Drive\Obsidian Vault backup" "G:\Trading Brain" /E /R:1 /W:1
# 2. re-arm the every-2-min backup task (from the recovery kit or cloned repo)
$tools = "H:\My Drive\ClaudeBackup\recovery-kit"   # or your cloned repo's tools\
$a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$tools\vault_backup.ps1`""
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "ObsidianVaultLiveSync" -Action $a -Trigger $t -Settings $s -Force
```
> Note: `vault_backup.ps1` expects the vault at `G:\Trading Brain` — edit the path in
> `vault_common.ps1` if the new machine uses a different location (or a Mac path).

## Notes & caveats
- **OAuth token is never restored** (machine-bound). Re-sign-in covers it.
- **Drive-letter / path changes:** sessions store their original working dir (e.g.
  `G:\fyers_data_pipeline`). If the new machine uses different paths (or it's a Mac —
  see the planned Google-Drive migration), the sidebar still lists them, but *resuming*
  a session needs that path to exist. Recreate the folders or re-clone the project repos
  to the same paths.
- **What's intentionally NOT backed up:** the 9.5 GB Desktop app cache/leveldb
  (regenerable) and the OAuth token.
- Keep this file current if the backup layout changes.
