# Deploy strangle fixes to VPS - ONE SSH session, no reconnect storms.
#   Copies 2 changed files to VPS, restarts dashboard, verifies.
#
# Usage: powershell -ExecutionPolicy Bypass -File .\deploy_strangle_fix.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log  = Join-Path $root "logs\deploy_strangle_fix.txt"
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
Start-Transcript -Path $log -Force | Out-Null

$vps = "Administrator@144.79.166.103"
$R   = "C:/trading/fyers_data_pipeline"

function Remote-PS([string]$script) {
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    ssh -o BatchMode=yes -o ConnectTimeout=20 $vps "powershell -NoProfile -EncodedCommand $enc"
}

# ---------------------------------------------------------------------------
Write-Host "`n1. Copying changed files to VPS via SCP..."
$key = Join-Path $env:USERPROFILE ".ssh\id_rsa"

$copies = @(
    @("$root\live_trading_options\strangle_strategy\live\control_flags.py", "$R/live_trading_options/strangle_strategy/live/control_flags.py"),
    @("$root\deployment\static\index.html", "$R/deployment/static/index.html")
)

foreach ($c in $copies) {
    $src = $c[0]; $dst = $c[1]
    scp -o BatchMode=yes -i $key -o StrictHostKeyChecking=no $src "${vps}:$dst"
    if ($LASTEXITCODE -ne 0) { Write-Host "   FAILED: $dst"; exit 1 }
    Write-Host "   OK: $(Split-Path $src -Leaf)"
}

# ---------------------------------------------------------------------------
Write-Host "`n2. Verifying files on VPS..."
$verify = Remote-PS @'
$root = 'C:/trading/fyers_data_pipeline'
$files = @(
    "$root/live_trading_options/strangle_strategy/live/control_flags.py",
    "$root/deployment/static/index.html"
)
foreach ($f in $files) {
    $d = [IO.File]::ReadAllBytes($f)
    $name = Split-Path $f -Leaf
    $flags = @()
    if ($f -match 'control_flags') {
        if ([BitConverter]::ToString($d).Contains('6B-69-6C-6C-5F-63-6C-65-61-72-65-64')) { $flags += 'stale-kill-fix' }
    }
    if ($f -match 'index\.html') {
        if ([BitConverter]::ToString($d).Contains('73-63-61-51-74-79-49-6E-70-75-74')) { $flags += 'scaQtyInput-cache-clear' }
    }
    Write-Host "$name ($($d.Length) bytes) $($flags -join ', ')"
}
'@
$verify | ForEach-Object { Write-Host "   $_" }

# ---------------------------------------------------------------------------
Write-Host "`n3. Restarting dashboard (loads new code)..."
$restart = Remote-PS @'
$root = 'C:/trading/fyers_data_pipeline'
$py = "$root/.venv/Scripts/python.exe"
$ctl = Join-Path $root 'deployment\vps_tasks\restart_server.bat'
if (Test-Path $ctl) {
    & cmd /c $ctl
    Write-Host 'restart_server.bat invoked'
} else {
    # Fallback: kill uvicorn on port 8801, it will be restarted by task scheduler
    $p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'uvicorn.*deployment\.main.*8801' }
    if ($p) { Stop-Process -Id $p.ProcessId -Force; Write-Host "killed uvicorn PID $($p.ProcessId)" }
    else { Write-Host 'no uvicorn on 8801 found' }
}
'@
$restart | ForEach-Object { Write-Host "   $_" }

# ---------------------------------------------------------------------------
Write-Host "`n4. Waiting for dashboard to come back (60s)..."
Start-Sleep -Seconds 60

$health = Remote-PS @'
try {
    $r = Invoke-WebRequest http://127.0.0.1:8801/ -UseBasicParsing -TimeoutSec 10
    Write-Host "HTTP $($r.StatusCode)"
} catch {
    Write-Host "NOT_UP: $($_.Exception.Message)"
}
'@
$health | ForEach-Object { Write-Host "   $_" }

Write-Host "`n>>> Done. Open https://dash.trading.contact and verify broker/qty settings."
Stop-Transcript | Out-Null
