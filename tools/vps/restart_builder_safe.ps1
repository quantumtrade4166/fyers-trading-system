# restart_builder_safe.ps1 -- update + restart ONLY the Strategy Builder, allowed in market hours
# ONLY when the builder holds nothing (no deployment that is open / pending / in flight).
# Arrives with builder2_bundle.zip on the Desktop. Never touches the dashboard or any engine.
$ErrorActionPreference = 'Stop'
$repo = 'C:\trading\fyers_data_pipeline'
$desk = 'C:\Users\Administrator\Desktop'
$py = "$repo\.venv\Scripts\python.exe"

$d = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 'http://127.0.0.1:47674/api/sb/deployments').Content | ConvertFrom-Json
$busy = @($d.deployments | Where-Object { $_.open_qty -gt 0 -or $_.status -in 'placing', 'closing', 'pending' })
if ($busy.Count -gt 0) { throw "builder holds $($busy.Count) live deployment(s) -- NOT restarting" }
"builder holds nothing -- safe to restart"

$bk = "$repo\_deploy_backups\builder_safe_" + (Get-Date -Format 'yyyyMMdd_HHmmss')
$tmp = "$desk\builder2_unpack"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive "$desk\builder2_bundle.zip" $tmp
Get-ChildItem $tmp -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($tmp.Length + 1); $dst = Join-Path $repo $rel
    if (Test-Path $dst) { $b = Join-Path $bk $rel; New-Item -ItemType Directory -Force (Split-Path $b) | Out-Null; Copy-Item $dst $b }
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Copy-Item $_.FullName $dst -Force; "   $rel"
}
Push-Location $repo
& $py -m py_compile live_trading_options\strategy_builder\brokers.py
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "syntax check failed -- restore from $bk" }
Pop-Location

Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*strategy_builder.service*' } |
    ForEach-Object { "   stop PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 3
schtasks /End /TN StrategyBuilder 2>$null | Out-Null
schtasks /Run /TN StrategyBuilder | Out-Null
foreach ($i in 1..30) { Start-Sleep 2; try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:47674/api/sb/health').StatusCode -eq 200) { break } } catch {} }
Start-Sleep 15
((Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 'http://127.0.0.1:47674/api/sb/brokers').Content | ConvertFrom-Json).brokers | ForEach-Object { "   $($_.id) -> $($_.status)" }
Remove-Item -Recurse -Force $tmp; Remove-Item -Force "$desk\builder2_bundle.zip"
Remove-Item -Force $MyInvocation.MyCommand.Path
