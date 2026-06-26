# claude_backup_common.ps1  --  Shared config + helpers for the Claude backup system.
# Dot-sourced by backup_claude.ps1 (daily full reconcile) and
# sync_claude_live.ps1 (continuous live mirror). Single source of truth.

# ---- Sources ----------------------------------------------------------------
$script:ClaudeCli  = Join-Path $env:USERPROFILE ".claude"
$script:Projects   = Join-Path $script:ClaudeCli "projects"
$script:AppData    = "C:\Users\PC\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude"
$script:Account    = "54037dfb-2850-4ab5-a96a-4bb6854d9966"
$script:Org        = "8d5e313b-51a3-4d77-8984-f01123ff97ed"
$script:OrgDir     = Join-Path $script:AppData ("claude-code-sessions\{0}\{1}" -f $script:Account, $script:Org)
$script:DesktopCfg = Join-Path $script:AppData "claude_desktop_config.json"

# Dirs inside ~/.claude that are regenerable noise -- never backed up.
$script:ExcludeDirs = @("shell-snapshots", "telemetry", "cache", "session-env", "statsig", "tasks")

# ---- Destinations: always local G:, plus Google Drive when mounted ----------
function Get-BackupBases {
    $bases = @("G:\ClaudeBackups")          # local second-drive copy (always)
    $gd = $null
    foreach ($c in @((Join-Path $env:USERPROFILE "My Drive"), "H:\My Drive", "G:\My Drive")) {
        if (Test-Path $c) { $gd = $c; break }
    }
    if (-not $gd) {
        Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue | ForEach-Object {
            if (($_.Description -match "Google") -or ($_.DisplayRoot -match "Google")) {
                $cand = Join-Path "$($_.Name):\" "My Drive"
                if (Test-Path $cand) { $gd = $cand }
            }
        }
    }
    if ($gd) { $bases += $gd }               # Google Drive (cloud) copy when present
    return $bases
}

# ---- One sync pass to every destination -------------------------------------
# $mirror = $true  -> robocopy /MIR (full reconcile, propagates deletions)
# $mirror = $false -> additive incremental (/E, never deletes; safe for live)
function Invoke-ClaudeSync {
    param([bool]$Mirror = $false, [scriptblock]$Logger = { param($m) Write-Output $m })

    $bases = Get-BackupBases
    $mode = @("/E"); if ($Mirror) { $mode = @("/MIR") }
    $rcArgs = $mode + @("/R:1", "/W:1", "/NFL", "/NDL", "/NP", "/NJH", "/NJS")

    foreach ($base in $bases) {
        $dest = Join-Path $base "ClaudeBackup"
        New-Item -ItemType Directory -Force -Path $dest | Out-Null

        # 1+2. transcripts + config
        $null = robocopy $script:ClaudeCli (Join-Path $dest "claude") /XD $script:ExcludeDirs @rcArgs
        $c1 = $LASTEXITCODE
        # 3. desktop sidebar index (tiny json files only)
        $null = robocopy $script:OrgDir (Join-Path $dest ("desktop-index\{0}\{1}" -f $script:Account, $script:Org)) "local_*.json" @rcArgs
        $c2 = $LASTEXITCODE
        # 4. desktop MCP config (single file; NOT the OAuth-bearing config.json)
        if (Test-Path $script:DesktopCfg) {
            $cfgDest = Join-Path $dest "desktop-config"
            New-Item -ItemType Directory -Force -Path $cfgDest | Out-Null
            Copy-Item $script:DesktopCfg (Join-Path $cfgDest "claude_desktop_config.json") -Force
        }
        # 5. CLI config files that live OUTSIDE ~/.claude (global state, MCP, trust)
        $homeDest = Join-Path $dest "home"
        New-Item -ItemType Directory -Force -Path $homeDest | Out-Null
        foreach ($hf in @(".claude.json", ".claude.json.backup", ".mcp.json")) {
            $hsrc = Join-Path $env:USERPROFILE $hf
            if (Test-Path $hsrc) { Copy-Item $hsrc (Join-Path $homeDest $hf) -Force }
        }
        # 6. Self-contained recovery kit: the scripts + runbook, so the backup can be
        #    restored using ONLY this folder (no GitHub access needed).
        $kitSrc = $PSScriptRoot
        if ($kitSrc -and (Test-Path $kitSrc)) {
            $null = robocopy $kitSrc (Join-Path $dest "recovery-kit") "*.ps1" "*.py" "*.md" /R:1 /W:1 /NFL /NDL /NP /NJH /NJS
        }
        $tag = if ($base -like "*My Drive*") { "GoogleDrive" } else { "local" }
        if ($c1 -ge 8 -or $c2 -ge 8) { & $Logger "ERROR sync->$tag ($dest) rc=$c1/$c2" }
        else { & $Logger "OK sync->$tag ($dest) rc=$c1/$c2" }
    }
}
