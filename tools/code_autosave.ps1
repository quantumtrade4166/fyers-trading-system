# code_autosave.ps1 -- saves code to GitHub automatically (trading project + JARVIS).
# Run daily 23:30 and at logon by scheduled task 'CodeAutoSave'.
#
# Trading project: commits CODE files only (same filter used for the manual save on
# 2026-10-09), never data/logs/secrets, and refuses to commit if anything in the change
# looks like a secret. JARVIS: pushes commits that are not on GitHub yet.

$ErrorActionPreference = 'Continue'
$log = 'D:\My Drive\PC Backup\code_autosave_log.txt'
$status = 'D:\My Drive\PC Backup\code_autosave_status.json'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Log($m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding utf8 }

$codeExt = '.py', '.ps1', '.bat', '.cmd', '.md', '.toml', '.ts', '.js', '.xml', '.html', '.css', '.vbs', '.sh', '.txt'
$skip = 'node_modules|^Bhavcopy/|^Bitcoin options data/|^ETF data/|^Nifty 500 Daily|^Nifty fno|^scratchpad/|^\.obsidian/|^\.idea/|^\.claude/|^\.tmp\.driveupload|/data/|live_state|/results/|ledger|/logs?/|^logs/|chart_history|\.min\.js$|/dist/|/build/|__pycache__|credential|creds|secret|token'
$secretPat = '(sk-[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(api[_-]?key|secret|password|totp|access[_-]?token)["'' ]*[:=][ ]*["''][A-Za-z0-9_.-]{16,}["''])'
$ok = $true

# --- trading project ---------------------------------------------------------
Push-Location 'G:\fyers_data_pipeline'
try {
    $changed = git status --porcelain --untracked-files=all | ForEach-Object {
        $p = ($_.Substring(3)).Trim('"'); if ($p -match ' -> ') { $p = ($p -split ' -> ')[1] }; $p }
    $pick = $changed | Where-Object {
        $ext = [IO.Path]::GetExtension($_).ToLower()
        ($codeExt -contains $ext -or ($ext -eq '.json' -and $_ -match '(config|package\.json|tsconfig|parameters|params)')) -and
        $_ -notmatch $skip -and ((-not (Test-Path -LiteralPath $_)) -or (Get-Item -LiteralPath $_).Length -lt 1MB) }
    if ($pick) {
        foreach ($p in $pick) { git add -A -- "$p" 2>$null }
        $hits = (git diff --cached -U0 | Select-String -Pattern '^\+' | Select-String -Pattern $secretPat).Count
        if ($hits -gt 0) {
            git reset -q
            Log "trading: NOT committed - $hits secret-like line(s) found; needs a human look"
            $ok = $false
        } else {
            $n = (git diff --cached --name-only).Count
            git -c user.name=quantumtrade4166 commit -q -m "Auto-save code $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
            Log "trading: committed $n file(s)"
        }
    }
    $ahead = git rev-list --count '@{u}..HEAD' 2>$null
    if ($ahead -and [int]$ahead -gt 0) {
        git push -q 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Log "trading: pushed $ahead commit(s)" } else { Log 'trading: PUSH FAILED'; $ok = $false }
    }
} finally { Pop-Location }

# --- JARVIS (push only; its commits are made deliberately) ---------------------
Push-Location 'G:\jarvis'
try {
    $ahead = git rev-list --count '@{u}..HEAD' 2>$null
    if ($ahead -and [int]$ahead -gt 0) {
        git push -q 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Log "jarvis: pushed $ahead commit(s)" } else { Log 'jarvis: PUSH FAILED'; $ok = $false }
    }
    $dirty = (git status --porcelain | Where-Object { $_ -notmatch '^\?\? data/' }).Count
    if ($dirty -gt 0) { Log "jarvis: $dirty uncommitted change(s) (covered by the Drive copy)" }
} finally { Pop-Location }

$state = @{ last_run = (Get-Date).ToString('o'); ok = $ok }
if ($ok) { $state.last_ok = $state.last_run }
elseif (Test-Path $status) { try { $prev = Get-Content $status -Raw | ConvertFrom-Json; if ($prev.last_ok) { $state.last_ok = $prev.last_ok } } catch { } }
$state | ConvertTo-Json | Set-Content $status -Encoding utf8
