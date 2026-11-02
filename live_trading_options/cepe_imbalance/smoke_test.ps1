$root = "C:\trading\fyers_data_pipeline"
$p = Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList "-m","live_trading_options.cepe_imbalance.service" -WorkingDirectory $root -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 30
try {
  $s = Invoke-RestMethod http://127.0.0.1:47690/state -TimeoutSec 10
  $s.underlyings | ForEach-Object { "{0,-9} ref {1} syn {2} atm {3} exp {4} in_session {5}" -f $_.name, $_.ref, $_.synthetic, $_.atm, $_.expiry, $_.session.in_session }
  "feed: " + ($s.feed | ConvertTo-Json -Compress)
  $o = Invoke-RestMethod http://127.0.0.1:47690/order/SILVERM -Method Post -ContentType "application/json" -Body '{"strike":184000,"kind":"CE","side":"SELL","lots":2}'
  "order: " + $o.symbol + " " + $o.side + " " + $o.lots + " @ " + $o.price
  $pos = Invoke-RestMethod http://127.0.0.1:47690/positions/SILVERM
  "positions: mtm " + ($pos.mtm | ConvertTo-Json -Compress) + " imb " + ($pos.imbalance | ConvertTo-Json -Compress)
  $x = Invoke-RestMethod http://127.0.0.1:47690/control/SILVERM -Method Post -ContentType "application/json" -Body '{"action":"exit_all"}'
  "exit: " + $x.exits.Count + " fills"
} catch { "ERR " + $_ }
Stop-Process -Id $p.Id -Force
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*cepe_imbalance.service*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "killed child " + $_.ProcessId }
Remove-Item "$root\live_trading_options\cepe_imbalance\data\books" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$root\live_trading_options\cepe_imbalance\data\history" -Recurse -Force -ErrorAction SilentlyContinue
Get-Content "$root\logs\cepe_imbalance.log" -Tail 6
