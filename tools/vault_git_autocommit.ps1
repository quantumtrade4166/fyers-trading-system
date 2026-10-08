# vault_git_autocommit.ps1
# Daily auto-commit of the Obsidian Trading Brain vault (local git time-machine).
# Registered as Windows Scheduled Task "VaultGitAutoCommit". Local git only (no push).
# Runs as the logged-in user so git identity + repo ownership match.

$ErrorActionPreference = "Stop"
$vault = "G:\Trading Brain"
$log   = "G:\fyers_data_pipeline\logs\vault_git_autocommit.log"

function Log($m) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Add-Content -Path $log -Encoding utf8
}

try {
    if (-not (Test-Path (Join-Path $vault ".git"))) { Log "SKIP: vault is not a git repo"; exit 0 }
    Set-Location $vault

    git add -A
    $changes = git status --porcelain
    if ([string]::IsNullOrWhiteSpace($changes)) { Log "no changes - nothing to commit"; exit 0 }

    $n = @($changes -split "`r?`n" | Where-Object { $_ -ne "" }).Count
    $msg = "vault auto-commit $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    git commit -m $msg | Out-Null
    Log "committed: $n file(s) changed"
}
catch {
    Log "ERROR: $_"
    exit 1
}
