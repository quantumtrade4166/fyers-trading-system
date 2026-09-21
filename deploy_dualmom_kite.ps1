# DualMom on the MAIN Kite account - install + Rs 6,00,000 deployment.
#
#  1. copies the code to the VPS (also carries today's Kotak fixes)
#  2. runs the tests ON THE VPS - stops if any fail
#  3. fixes the DualMom Windows task (no 72h kill, 5-min watchdog), adds Kite to
#     the Google-Drive backup, restarts ONLY the DualMom service
#  4. checks Kotak is healthy
#  5. PREVIEWS the Kite basket for Rs 6,00,000 - nothing sent
#  6. asks you to type DEPLOY. Anything else = nothing is sent.
#  7. places the orders, then prints every entry price and saves them to
#     logs\dualmom_kite_entries_<date>.csv
#
# Strangle, DN engine and the dashboard process are not touched. DualMom never
# logs in to Kite - it reads the token the VPS already made this morning.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log  = Join-Path $root "logs\deploy_dualmom_kite.txt"
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
Start-Transcript -Path $log -Force | Out-Null

$CAPITAL = 600000
$vps = "Administrator@144.79.166.103"
$R   = "C:/Users/Administrator/Desktop/fyers_data_pipeline_git"

function Remote-PS([string]$script) {
    $script = "`$ProgressPreference = 'SilentlyContinue'`n" + $script
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    ssh -o BatchMode=yes -o ConnectTimeout=20 $vps "powershell -NoProfile -EncodedCommand $enc"
}

# Call the DualMom service ON the VPS; the reply travels base64 so no character is mangled.
function DM([string]$method, [string]$path, [string]$body = "", [int]$timeout = 180) {
    $b64body = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($body))
    $out = Remote-PS @"
try {
  `$u = 'http://127.0.0.1:8010$path'
  if ('$method' -eq 'POST') {
    `$b = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$b64body'))
    `$r = Invoke-WebRequest `$u -Method POST -Body ([Text.Encoding]::UTF8.GetBytes(`$b)) -ContentType 'application/json' -UseBasicParsing -TimeoutSec $timeout
  } else {
    `$r = Invoke-WebRequest `$u -UseBasicParsing -TimeoutSec $timeout
  }
  'B64:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(`$r.Content))
} catch { 'ERR:' + `$_.Exception.Message }
"@
    $line = ($out | Where-Object { $_ -match '^(B64|ERR):' } | Select-Object -Last 1)
    if (-not $line) { return [pscustomobject]@{ ok = $false; error = "no reply: $out" } }
    if ($line.StartsWith('ERR:')) { return [pscustomobject]@{ ok = $false; error = $line.Substring(4) } }
    $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line.Substring(4)))
    return ($json | ConvertFrom-Json)
}

function Stop-Here([string]$why) {
    Write-Host "`n>>> STOPPED: $why" -ForegroundColor Red
    Write-Host ">>> Nothing was sent to Kite. Tell Claude (the log is saved)."
    Stop-Transcript | Out-Null
    exit 1
}

# -----------------------------------------------------------------------------
Write-Host "`n1. Copying files to the VPS..."
$xml16 = Join-Path $env:TEMP "DualMomService16.xml"
Get-Content (Join-Path $root "deployment\vps_tasks\DualMomService.xml") -Raw | Out-File -FilePath $xml16 -Encoding Unicode
Remote-PS "New-Item -ItemType Directory -Force -Path '$R/deployment/dualmom_kite/tests' | Out-Null" | Out-Null
$copies = @(
  @("$root\deployment\dualmom_kite\__init__.py", "$root\deployment\dualmom_kite\config.py", "$root\deployment\dualmom_kite\kite_equity.py",
    "$root\deployment\dualmom_kite\ledger.py", "$root\deployment\dualmom_kite\book.py", "$root\deployment\dualmom_kite\engine.py", "$R/deployment/dualmom_kite/"),
  @("$root\deployment\dualmom_kite\tests\__init__.py", "$root\deployment\dualmom_kite\tests\test_kite.py", "$R/deployment/dualmom_kite/tests/"),
  @("$root\deployment\dualmom_kite_api.py", "$root\deployment\dualmom_service.py", "$root\deployment\dualmom_live_api.py", "$R/deployment/"),
  @("$root\deployment\dualmom_live\rebalance.py", "$root\deployment\dualmom_live\kotak_equity.py", "$root\deployment\dualmom_live\source_ip.py", "$R/deployment/dualmom_live/"),
  @("$root\deployment\dualmom_live\tests\test_offline.py", "$R/deployment/dualmom_live/tests/"),
  @("$root\deployment\static\index.html", "$R/deployment/static/"),
  @("$root\deployment\vps_tasks\DualMomService.xml", "$root\deployment\vps_tasks\fix_intraday_20260921.py", "$root\deployment\vps_tasks\patch_backup_kite.py", "$R/deployment/vps_tasks/")
)
foreach ($c in $copies) {
    $dest = $c[-1]; $src = $c[0..($c.Count - 2)]
    scp -o BatchMode=yes @src "${vps}:$dest"
    if ($LASTEXITCODE -ne 0) { Stop-Here "copy failed ($dest)" }
}
scp -o BatchMode=yes $xml16 "${vps}:C:/Users/Administrator/DualMomService16.xml"
if ($LASTEXITCODE -ne 0) { Stop-Here "copy failed (task xml)" }
Write-Host "   copied"

Write-Host "`n2. Tests on the VPS..."
$t = Remote-PS @'
Set-Location C:\Users\Administrator\Desktop\fyers_data_pipeline_git
foreach ($m in 'deployment.dualmom_kite.tests.test_kite','deployment.dualmom_live.tests.test_offline','deployment.dualmom_live.tests.test_ledger','deployment.dualmom_live.tests.test_source_ip') {
  $last = (.venv\Scripts\python.exe -m $m 2>&1 | Select-String 'passed, \d+ failed' | Select-Object -Last 1)
  if (-not $last) { $last = 'NO RESULT LINE' }
  "$m :: $last"
}
'@
$t | ForEach-Object { Write-Host "   $_" }
$okLines = @($t | Where-Object { $_ -match ' :: .*\d+ passed, 0 failed' }).Count
if ($okLines -ne 4) { Stop-Here "a test failed on the VPS" }

Write-Host "`n3. Task fix, Drive backup, today's bad Kotak readings..."
Remote-PS @'
schtasks /Create /TN DualMomService /XML C:\Users\Administrator\DualMomService16.xml /F
Remove-Item C:\Users\Administrator\DualMomService16.xml -Force
$py = 'C:\Users\Administrator\Desktop\fyers_data_pipeline_git\.venv\Scripts\python.exe'
& $py C:\Users\Administrator\Desktop\fyers_data_pipeline_git\deployment\vps_tasks\fix_intraday_20260921.py
& $py C:\Users\Administrator\Desktop\fyers_data_pipeline_git\deployment\vps_tasks\patch_backup_kite.py
'@ | ForEach-Object { Write-Host "   $_" }

Write-Host "`n4. Restarting the DualMom service only..."
Remote-PS @'
$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -match 'start_dualmom_service\.bat' -or $_.CommandLine -match 'deployment\.dualmom_service') }
foreach ($x in $p) { Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
schtasks /Run /TN DualMomService | Out-Null
'@ | Out-Null
$up = $false
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Seconds 10
    $h = DM GET "/health" "" 20
    if ($h.ok -and (($h.jobs | ForEach-Object { $_.id }) -contains 'dmk_rebalance')) { $up = $true; break }
}
if (-not $up) { Stop-Here "DualMom service did not come back with the Kite jobs" }
Write-Host "   service up - Kotak + Kite jobs scheduled"

Write-Host "`n5. Kotak check..."
$kb = DM GET "/api/dualmom/live/book?fresh=1" "" 150
if ($kb.ok) {
    Write-Host ("   Kotak: NAV Rs {0:N0}  cash Rs {1:N0}  {2} positions  recon={3}" -f $kb.nav, $kb.cash, $kb.positions, $kb.reconciliation.ok)
    if ($kb.cash -gt 100000) { Write-Host "   WARNING: Kotak cash still includes margin - tell Claude" -ForegroundColor Yellow }
} else { Write-Host "   Kotak book: $($kb.error)" -ForegroundColor Yellow }

Write-Host "`n6. Kite status..."
$st = DM GET "/api/dualmom/live/kite/status" "" 60
if (-not $st.broker.connected) { Stop-Here "Kite not connected: $($st.broker.error)" }
Write-Host "   Kite connected (today's token, no login)"
if (-not $st.can_deploy) { Stop-Here ("Kite deploy blocked: " + ($st.deploy_blocked_by -join '; ')) }

Write-Host "`n7. PREVIEW - Rs $CAPITAL on Kite (nothing is sent)..."
$pl = DM POST "/api/dualmom/live/kite/plan" ('{"capital": ' + $CAPITAL + '}') 300
if (-not $pl.ok) { Stop-Here "preview failed: $($pl.error)" }
$p = $pl.run.plan
$buys = @($p.buys)
Write-Host ("   signal {0} on {1}  |  {2} buys  |  {3} sells" -f $p.signal, $p.signal_date, $buys.Count, @($p.sells).Count)
Write-Host ""
Write-Host ("   {0,-16} {1,6} {2,11} {3,11} {4,12}" -f "Symbol", "Qty", "Price", "Limit", "Value")
foreach ($o in $buys) {
    Write-Host ("   {0,-16} {1,6} {2,11:N2} {3,11:N2} {4,12:N0}" -f $o.trading_symbol, $o.qty, $o.mark, $o.limit_preview, ($o.qty * $o.mark))
}
$bv = ($buys | Measure-Object -Property value -Sum).Sum
$m = $p.account_margin
Write-Host ""
Write-Host ("   Basket value     Rs {0,12:N0}   ({1:N1}% of Rs {2:N0})" -f $bv, ($bv / $CAPITAL * 100), $CAPITAL)
Write-Host ("   Kite free margin Rs {0,12:N0}   now (whole account)" -f $m.net)
Write-Host ("   After the basket Rs {0,12:N0}   left for the strangle / anything else" -f $m.free_after)
foreach ($w in @($pl.run.warnings)) { if ($w) { Write-Host "   WARNING: $w" -ForegroundColor Yellow } }
foreach ($b in @($p.blocked)) { if ($b) { Write-Host "   BLOCKED: $b" -ForegroundColor Red } }
if (-not $p.safe) { Stop-Here "plan failed its safety checks" }
$bad = @($buys | Where-Object { $_.resolve_error })
if ($bad.Count) { Write-Host ("   will be SKIPPED (not on Kite): " + (($bad | ForEach-Object { $_.symbol }) -join ', ')) -ForegroundColor Yellow }

Write-Host ""
$ans = Read-Host "Type DEPLOY to place these $($buys.Count) REAL orders on Kite (anything else cancels)"
if ($ans -ne 'DEPLOY') {
    Write-Host "`n>>> Cancelled. Nothing was sent."
    Stop-Transcript | Out-Null
    exit 0
}

Write-Host "`n8. Placing orders on Kite (about 1-2 minutes)..."
$dp = DM POST "/api/dualmom/live/kite/deploy" '{"confirm": "DEPLOY"}' 600
if (-not $dp.ok) { Stop-Here "deploy refused: $($dp.error)" }
$orders = @($dp.run.orders)
foreach ($l in @($dp.run.log)) { Write-Host "   $l" }
$filled = @($orders | Where-Object { $_.status -eq 'FILLED' })
Write-Host ("`n   {0} of {1} orders FILLED" -f $filled.Count, $orders.Count)
if ($dp.halted) { Write-Host "   HALTED: $($dp.halted)" -ForegroundColor Red }

Write-Host "`n9. Entry prices (from Kite's own fills)..."
Start-Sleep -Seconds 5
DM POST "/api/dualmom/live/kite/ledger/capture" "{}" 120 | Out-Null
$bk = DM GET "/api/dualmom/live/kite/book?fresh=1" "" 150
$csv = Join-Path $root ("logs\dualmom_kite_entries_{0}.csv" -f (Get-Date -Format 'yyyyMMdd'))
if ($bk.ok) {
    Write-Host ("   {0,-14} {1,6} {2,11} {3,12}" -f "Symbol", "Qty", "Entry", "Invested")
    foreach ($r in @($bk.rows | Sort-Object symbol)) {
        Write-Host ("   {0,-14} {1,6} {2,11:N2} {3,12:N0}" -f $r.symbol, $r.qty, $r.avg_price, $r.cost)
    }
    @($bk.rows | Sort-Object symbol | Select-Object symbol, trading_symbol, qty, avg_price, cost, first_fill) |
        Export-Csv -Path $csv -NoTypeInformation -Encoding UTF8
    Write-Host ("`n   DualMom on Kite: invested Rs {0:N0}  own cash Rs {1:N0}  NAV Rs {2:N0}  {3} positions  recon={4}" -f `
        $bk.cost_basis, $bk.cash, $bk.nav, $bk.positions, $bk.reconciliation.ok)
    Write-Host "   Entry prices saved: $csv"
} else { Write-Host "   book: $($bk.error)" -ForegroundColor Yellow }

if ($filled.Count -eq $orders.Count -and -not $dp.halted) {
    Write-Host "`n>>> DEPLOYED. Dashboard -> DualMom -> Kite live. Tell Claude ""deployed""." -ForegroundColor Green
} else {
    Write-Host "`n>>> NOT everything filled - tell Claude (do NOT run this again today)." -ForegroundColor Yellow
}
Stop-Transcript | Out-Null
