# DualMom: client ledger + live portfolio dashboard (equity & drawdown curves).
# Copies the new/changed files, restarts ONLY the DualMom service (the dashboard
# process and the strangle are not touched - index.html is a static file the open
# page reloads by itself), then verifies against the live account.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log  = Join-Path $root "logs\dualmom_portfolio_update.txt"
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
Start-Transcript -Path $log -Force | Out-Null

$vps = "Administrator@144.79.166.103"
$R   = "C:/Users/Administrator/Desktop/fyers_data_pipeline_git"

function Remote-PS([string]$script) {
    $script = "`$ProgressPreference = 'SilentlyContinue'`n" + $script
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    ssh -o BatchMode=yes $vps "powershell -NoProfile -EncodedCommand $enc"
}

Write-Host "`n1. Copying files..."
$ok = $true
scp -o BatchMode=yes "$root\deployment\dualmom_live\ledger.py" "$root\deployment\dualmom_live\book.py" "$root\deployment\dualmom_live\config.py" "${vps}:$R/deployment/dualmom_live/"; if ($LASTEXITCODE -ne 0) { $ok = $false }
scp -o BatchMode=yes "$root\deployment\dualmom_live_api.py" "$root\deployment\dualmom_service.py" "${vps}:$R/deployment/"; if ($LASTEXITCODE -ne 0) { $ok = $false }
scp -o BatchMode=yes "$root\deployment\static\index.html" "${vps}:$R/deployment/static/"; if ($LASTEXITCODE -ne 0) { $ok = $false }
if (-not $ok) { Write-Host "COPY FAILED - stop here, tell Claude."; Stop-Transcript | Out-Null; exit 1 }

Write-Host "`n2. Removing the test sandbox (it held copies of client data)..."
Remote-PS @'
if (Test-Path "C:\Users\Administrator\dm_sandbox") { Remove-Item "C:\Users\Administrator\dm_sandbox" -Recurse -Force; "sandbox removed" } else { "no sandbox" }
'@

Write-Host "`n3. Restarting the DualMom service only..."
Remote-PS @'
$c = Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue
foreach ($x in $c) { Stop-Process -Id $x.OwningProcess -Force; "stopped pid $($x.OwningProcess)" }
'@

Write-Host "`n4. Waiting for it, then for the first ledger capture (about 90 seconds)..."
Start-Sleep -Seconds 90

Write-Host "`n5. Verifying against the live account..."
$v = Remote-PS @'
function Get-J($u) { try { (Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 120).Content | ConvertFrom-Json } catch { $null } }
$b = "http://127.0.0.1:8010/api/dualmom/live"
$h = Get-J "http://127.0.0.1:8010/health"
$book = Get-J "$b/book?fresh=1"
$fills = Get-J "$b/ledger?kind=fills&limit=1"
$ver = Get-J "$b/ledger/verify"
$eq = Get-J "$b/equity"
"JOBS=" + (($h.jobs | ForEach-Object { $_.id }) -join ",")
"BOOK_OK=$($book.ok) POSITIONS=$($book.positions) RECON=$($book.reconciliation.ok) NAV=$($book.nav) CASH=$($book.cash) MTM=$($book.unrealized)"
"FILLS=$($fills.total)"
"CHAIN_OK=$($ver.ok)"
"EQUITY_OK=$($eq.ok) POINTS=$($eq.points)"
'@
$v | ForEach-Object { Write-Host "   $_" }
$txt = "$v"
$pass = ($txt -match 'dm_eod_snapshot') -and ($txt -match 'BOOK_OK=True') -and ($txt -match 'RECON=True') -and ($txt -match 'CHAIN_OK=True') -and ($txt -match 'EQUITY_OK=True') -and ($txt -match 'FILLS=\d{2,}')

if ($pass) { Write-Host "`n>>> UPDATED. Open the dashboard -> DualMom -> Live, then tell Claude ""updated""." }
else       { Write-Host "`n>>> NOT CONFIRMED - tell Claude ""not updated"" (the log is saved)." }
Stop-Transcript | Out-Null
