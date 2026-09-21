# DualMom service fix (21-Sep-2026)
#  - cash no longer counts Kotak's margin-on-holdings (NAV showed 17.2L / +72%)
#  - every Kotak call now has a timeout (a stuck call froze the whole page)
#  - error text can't crash the endpoint
#  - Windows task: no 72-hour kill (that is what stopped it on 16-Sep 21:48)
#    + re-checks every 5 min and restarts it if it's down
# Touches ONLY the DualMom service. Dashboard, strangle and other engines untouched.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log  = Join-Path $root "logs\fix_dualmom_service.txt"
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
Start-Transcript -Path $log -Force | Out-Null

$vps = "Administrator@144.79.166.103"
$R   = "C:/Users/Administrator/Desktop/fyers_data_pipeline_git"

function Remote-PS([string]$script) {
    $script = "`$ProgressPreference = 'SilentlyContinue'`n" + $script
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    ssh -o BatchMode=yes -o ConnectTimeout=20 $vps "powershell -NoProfile -EncodedCommand $enc"
}

Write-Host "`n1. Copying files..."
$ok = $true
$xml16 = Join-Path $env:TEMP "DualMomService16.xml"
Get-Content (Join-Path $root "deployment\vps_tasks\DualMomService.xml") -Raw | Out-File -FilePath $xml16 -Encoding Unicode
scp -o BatchMode=yes "$root\deployment\dualmom_live\kotak_equity.py" "$root\deployment\dualmom_live\source_ip.py" "${vps}:$R/deployment/dualmom_live/"; if ($LASTEXITCODE -ne 0) { $ok = $false }
scp -o BatchMode=yes "$root\deployment\dualmom_live\tests\test_offline.py" "${vps}:$R/deployment/dualmom_live/tests/"; if ($LASTEXITCODE -ne 0) { $ok = $false }
scp -o BatchMode=yes "$root\deployment\dualmom_live_api.py" "$root\deployment\dualmom_service.py" "${vps}:$R/deployment/"; if ($LASTEXITCODE -ne 0) { $ok = $false }
scp -o BatchMode=yes "$root\deployment\vps_tasks\DualMomService.xml" "$root\deployment\vps_tasks\fix_intraday_20260921.py" "${vps}:$R/deployment/vps_tasks/"; if ($LASTEXITCODE -ne 0) { $ok = $false }
scp -o BatchMode=yes $xml16 "${vps}:C:/Users/Administrator/DualMomService16.xml"; if ($LASTEXITCODE -ne 0) { $ok = $false }
if (-not $ok) { Write-Host "COPY FAILED - stop here, tell Claude."; Stop-Transcript | Out-Null; exit 1 }

Write-Host "`n2. Tests on the VPS..."
Remote-PS @'
Set-Location C:\Users\Administrator\Desktop\fyers_data_pipeline_git
.venv\Scripts\python.exe -m deployment.dualmom_live.tests.test_offline 2>&1 | Select-Object -Last 1
.venv\Scripts\python.exe -m deployment.dualmom_live.tests.test_source_ip 2>&1 | Select-Object -Last 1
'@

Write-Host "`n3. Fixing the Windows task (no 72h kill, 5-min watchdog)..."
Remote-PS @'
schtasks /Create /TN DualMomService /XML C:\Users\Administrator\DualMomService16.xml /F
Remove-Item C:\Users\Administrator\DualMomService16.xml -Force
'@

Write-Host "`n4. Removing today's wrong readings (margin counted as cash)..."
Remote-PS @'
C:\Users\Administrator\Desktop\fyers_data_pipeline_git\.venv\Scripts\python.exe C:\Users\Administrator\Desktop\fyers_data_pipeline_git\deployment\vps_tasks\fix_intraday_20260921.py
'@

Write-Host "`n5. Restarting the DualMom service only..."
Remote-PS @'
# stop the old supervisor loop AND its python, so exactly one copy runs under the new task
$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -match 'start_dualmom_service\.bat' -or $_.CommandLine -match 'deployment\.dualmom_service') }
foreach ($x in $p) { Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue; "stopped pid $($x.ProcessId)" }
Start-Sleep -Seconds 3
schtasks /Run /TN DualMomService
'@

Write-Host "`n6. Waiting 75 seconds for it to come back..."
Start-Sleep -Seconds 75

Write-Host "`n7. Verifying..."
$v = Remote-PS @'
function Get-J($u) { try { (Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 150).Content | ConvertFrom-Json } catch { $null } }
$b = "http://127.0.0.1:8010/api/dualmom/live"
$h = Get-J "http://127.0.0.1:8010/health"
$book = Get-J "$b/book?fresh=1"
$ver = Get-J "$b/ledger/verify"
"HEALTH=$($h.ok)"
"BOOK_OK=$($book.ok) POSITIONS=$($book.positions) RECON=$($book.reconciliation.ok) NAV=$($book.nav) CASH=$($book.cash) RETURN=$($book.total_return_pct)"
"CHAIN_OK=$($ver.ok)"
$x = schtasks /Query /TN DualMomService /XML
"TIMELIMIT=" + ((($x -join '') -match '<ExecutionTimeLimit>PT0S') -as [string])
"WATCHDOG=" + ((($x -join '') -match '<Interval>PT5M') -as [string])
'@
$v | ForEach-Object { Write-Host "   $_" }
$txt = "$v"
$cashOk = $false
if ($txt -match 'CASH=([\d\.]+)') { $cashOk = ([double]$Matches[1] -lt 100000) }
$pass = ($txt -match 'BOOK_OK=True') -and ($txt -match 'POSITIONS=40') -and ($txt -match 'RECON=True') -and ($txt -match 'CHAIN_OK=True') -and $cashOk -and ($txt -match 'TIMELIMIT=True') -and ($txt -match 'WATCHDOG=True')

if ($pass) { Write-Host "`n>>> FIXED. Refresh the dashboard -> DualMom -> Live, then tell Claude ""fixed""." }
else       { Write-Host "`n>>> NOT CONFIRMED - tell Claude ""not fixed"" (the log is saved)." }
Stop-Transcript | Out-Null
