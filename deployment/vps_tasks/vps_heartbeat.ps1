# vps_heartbeat.ps1 -- pings Healthchecks.io only when the dashboard is actually healthy.
# Runs as SYSTEM every 5 min (task VPSHealthHeartbeat). If the dashboard is down OR the
# whole VPS is down, pings stop -> Healthchecks.io alerts you. App-crash sends /fail (fast).
$ping='https://hc-ping.com/aa9ad008-8a13-4036-b377-118034126494'
try {
  $r = Invoke-WebRequest -Uri 'http://localhost:8000/' -UseBasicParsing -TimeoutSec 15
  if ($r.StatusCode -eq 200) {
    Invoke-WebRequest -Uri $ping -UseBasicParsing -TimeoutSec 15 | Out-Null
  } else {
    Invoke-WebRequest -Uri "$ping/fail" -UseBasicParsing -TimeoutSec 15 | Out-Null
  }
} catch {
  try { Invoke-WebRequest -Uri "$ping/fail" -UseBasicParsing -TimeoutSec 15 | Out-Null } catch {}
}
