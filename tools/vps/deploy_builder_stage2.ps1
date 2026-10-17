# deploy_builder_stage2.ps1 -- Strategy Builder stage 2 (broker sessions, deploy, positions).
#   scp builder2_bundle.zip deploy_builder_stage2.ps1 vps:C:/Users/Administrator/Desktop/
#   ssh vps "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Administrator\Desktop\deploy_builder_stage2.ps1"
# Restarts ONLY the StrategyBuilder process (never the dashboard or an engine), and refuses in market hours.
# Live orders stay impossible until the owner ARMS the desk on the page.
$ErrorActionPreference = 'Stop'
$repo = 'C:\trading\fyers_data_pipeline'
$desk = 'C:\Users\Administrator\Desktop'
$py = "$repo\.venv\Scripts\python.exe"
$bk = "$repo\_deploy_backups\builder2_" + (Get-Date -Format 'yyyyMMdd_HHmmss')

$ist = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), 'India Standard Time')
if ($ist.DayOfWeek -ne 'Saturday' -and $ist.DayOfWeek -ne 'Sunday' -and $ist.TimeOfDay -ge [TimeSpan]'09:00' -and $ist.TimeOfDay -le [TimeSpan]'15:35') {
    throw "market hours ($ist) -- deploy after 15:35"
}

"== 1. unpack (backup -> $bk)"
$tmp = "$desk\builder2_unpack"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive "$desk\builder2_bundle.zip" $tmp
Get-ChildItem $tmp -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($tmp.Length + 1)
    $dst = Join-Path $repo $rel
    if (Test-Path $dst) { $b = Join-Path $bk $rel; New-Item -ItemType Directory -Force (Split-Path $b) | Out-Null; Copy-Item $dst $b }
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Copy-Item $_.FullName $dst -Force
    "   $rel"
}

"== 2. tests on the VPS"
Push-Location $repo
& $py -m py_compile live_trading_options\strategy_builder\service.py live_trading_options\strategy_builder\orders.py live_trading_options\strategy_builder\brokers.py
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "syntax check failed -- restore from $bk" }
& $py -m live_trading_options.strategy_builder.test_greeks 2>&1 | Select-Object -Last 1
& $py -m live_trading_options.strategy_builder.test_orders 2>&1 | Select-Object -Last 1
& $py -c "import live_trading_options.strategy_builder.brokers as B; print('brokers module ok', [a['id'] for a in B.ACCOUNTS])"
Pop-Location

"== 3. restart the StrategyBuilder process only"
$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*strategy_builder.service*' }
foreach ($p in $procs) { "   stop PID $($p.ProcessId)"; Stop-Process -Id $p.ProcessId -Force }
Start-Sleep 3
schtasks /End /TN StrategyBuilder 2>$null | Out-Null
schtasks /Run /TN StrategyBuilder | Out-Null
$ok = $false
foreach ($i in 1..30) {
    Start-Sleep 2
    try { $h = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:47674/api/sb/health'; if ($h.StatusCode -eq 200) { $ok = $true; break } } catch {}
}
if (-not $ok) { "   !! builder did not come back -- logs\strategy_builder.log" } else { "   builder up" }
Start-Sleep 12
foreach ($u in 'brokers', 'control', 'deployments') {
    try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 "http://127.0.0.1:8000/api/sb/$u"; "   via dashboard /api/sb/$u -> $($r.Content.Substring(0, [Math]::Min(700, $r.Content.Length)))" } catch { "   !! /api/sb/$u : $_" }
}

"== 4. clean up"
Remove-Item -Recurse -Force $tmp
Remove-Item -Force "$desk\builder2_bundle.zip"
"DEPLOY DONE (backup: $bk)"
Remove-Item -Force $MyInvocation.MyCommand.Path
