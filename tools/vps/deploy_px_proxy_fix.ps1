# deploy_px_proxy_fix.ps1 -- dashboard proxy must forward ?px=1 on POSTs (practice orders were reaching the
# LIVE desk). Copies main.py + builder.html + service.py, restarts the DASHBOARD only (never in market
# hours), and proves a practice POST now reaches the practice desk. service.py takes effect at the
# builder's next restart (it is the second, belt-and-braces layer) so an open practice session survives.
$ErrorActionPreference = 'Stop'
$repo = 'C:\trading\fyers_data_pipeline'
$desk = 'C:\Users\Administrator\Desktop'
$py = "$repo\.venv\Scripts\python.exe"
$bk = "$repo\_deploy_backups\pxfix_" + (Get-Date -Format 'yyyyMMdd_HHmmss')
$ist = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), 'India Standard Time')
if ($ist.DayOfWeek -ne 'Saturday' -and $ist.DayOfWeek -ne 'Sunday' -and $ist.TimeOfDay -ge [TimeSpan]'09:00' -and $ist.TimeOfDay -le [TimeSpan]'15:35') { throw "market hours -- not restarting the dashboard" }

New-Item -ItemType Directory -Force $bk | Out-Null
foreach ($f in 'deployment\main.py', 'deployment\static\builder.html', 'live_trading_options\strategy_builder\service.py') {
    $d = Join-Path $bk $f; New-Item -ItemType Directory -Force (Split-Path $d) | Out-Null
    Copy-Item "$repo\$f" $d
    Copy-Item "$desk\$(Split-Path $f -Leaf)" "$repo\$f" -Force
    "   $f"
}
& $py -m py_compile "$repo\deployment\main.py" "$repo\live_trading_options\strategy_builder\service.py"
if ($LASTEXITCODE -ne 0) { throw "syntax check failed -- restore from $bk" }

$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*uvicorn deployment.main*' }
foreach ($p in $procs) { "   stop dashboard PID $($p.ProcessId)"; Stop-Process -Id $p.ProcessId -Force }
Start-Sleep 4
schtasks /Run /TN PairsDashboard | Out-Null
$ok = $false
foreach ($i in 1..45) { Start-Sleep 2; try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:8000/api/sb/health').StatusCode -eq 200) { $ok = $true; break } } catch {} }
if (-not $ok) { throw "dashboard did not come back -- restore from $bk" }
"   dashboard up"
# proof: an empty deploy with ?px=1 must be answered by the PRACTICE desk (never the live desk's "Market is closed")
try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Method POST -ContentType 'application/json' -Body '{}' 'http://127.0.0.1:8000/api/sb/deploy?px=1'; $c = $r.Content } catch { $c = $_.ErrorDetails.Message }
"   practice POST answered: $c"
if ($c -match 'Market is closed') { "   !! STILL REACHING THE LIVE DESK" } else { "   OK -- practice POSTs reach the practice desk" }
foreach ($f in 'main.py', 'builder.html', 'service.py') { Remove-Item -Force "$desk\$f" }
Remove-Item -Force $MyInvocation.MyCommand.Path
