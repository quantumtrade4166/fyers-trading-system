# vault_common.ps1  --  Shared config/helpers for backing up the Obsidian vault
# "Trading Brain" to Google Drive. Dot-sourced by vault_backup.ps1 (daily reconcile)
# and vault_sync_live.ps1 (continuous live mirror).

$script:VaultSrc = "G:\Trading Brain"

# Obsidian noise to skip (changes on every click; not worth syncing).
# Keep the rest of .obsidian (themes, appearance, plugins, hotkeys) so a restore
# looks identical.
$script:VaultExcludeFiles = @("workspace.json", "workspace-mobile.json", "workspace.json.bak")
$script:VaultExcludeDirs  = @(".trash")

# Destination base = Google Drive "My Drive". Returns $null if Drive isn't mounted.
function Get-VaultDest {
    $gd = $null
    foreach ($c in @((Join-Path $env:USERPROFILE "My Drive"), "H:\My Drive", "G:\My Drive")) {
        if (Test-Path $c) { $gd = $c; break }
    }
    if (-not $gd) {
        Get-PSDrive -PSProvider FileSystem -EA SilentlyContinue | ForEach-Object {
            if (($_.Description -match "Google") -or ($_.DisplayRoot -match "Google")) {
                $cand = Join-Path "$($_.Name):\" "My Drive"
                if (Test-Path $cand) { $gd = $cand }
            }
        }
    }
    if ($gd) { return (Join-Path $gd "Obsidian Vault backup") }
    return $null
}

# One sync pass.  $Mirror=$true -> /MIR (propagates deletions); $false -> additive /E.
function Invoke-VaultSync {
    param([bool]$Mirror = $false, [scriptblock]$Logger = { param($m) Write-Output $m })

    if (-not (Test-Path $script:VaultSrc)) { & $Logger "ERROR: vault source missing: $script:VaultSrc"; return }
    $dest = Get-VaultDest
    if (-not $dest) { & $Logger "WARN: Google Drive not mounted; skipping (will sync once Drive is back)"; return }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null

    $mode = if ($Mirror) { "/MIR" } else { "/E" }
    $null = robocopy $script:VaultSrc $dest $mode /R:1 /W:1 /NFL /NDL /NP /NJH /NJS `
        /XF workspace.json workspace-mobile.json workspace.json.bak /XD .trash
    $code = $LASTEXITCODE
    if ($code -ge 8) { & $Logger "ERROR vault sync rc=$code -> $dest" }
    else { & $Logger "OK vault sync rc=$code -> $dest" }
}
