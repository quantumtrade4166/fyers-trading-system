# vps_heartbeat.ps1 -- pings Healthchecks.io only when the VPS is actually healthy.
# Runs as SYSTEM every 5 min (task VPSHealthHeartbeat). Healthchecks.io emails the owner when:
#   - the whole VPS is down (pings stop), or
#   - the dashboard is down (/fail), or
#   - the Google Drive backup has not succeeded for over an hour (/fail) -- added 2026-10-11:
#     the backup needs Administrator signed in with Google Drive running, and the old
#     warning ran on the home PC, which is often off now.
# The reason is sent as the ping body, so the alert email says what is wrong.
$ping = 'https://hc-ping.com/aa9ad008-8a13-4036-b377-118034126494'
$backupStatus = 'C:\trading\ops\vps_backup_status.json'
$maxBackupAgeMin = 60

function Send($url, $body) {
  try { Invoke-WebRequest -Uri $url -Method Post -Body $body -UseBasicParsing -TimeoutSec 15 | Out-Null } catch {}
}

$problems = @()

try {
  $r = Invoke-WebRequest -Uri 'http://localhost:8000/' -UseBasicParsing -TimeoutSec 15
  if ($r.StatusCode -ne 200) { $problems += "dashboard answered HTTP $($r.StatusCode)" }
} catch {
  $problems += "dashboard not responding: $($_.Exception.Message)"
}

try {
  $s = Get-Content $backupStatus -Raw | ConvertFrom-Json
  $last = if ($s.last_ok) { [datetime]$s.last_ok } else { $null }
  if (-not $last) {
    $problems += 'Google Drive backup has never succeeded'
  } else {
    $age = [int]((Get-Date) - $last).TotalMinutes
    if ($age -gt $maxBackupAgeMin) {
      $why = if ($s.error -eq 'drive_not_mounted') { ' - Google Drive is not running: is Administrator signed in on the VPS?' } else { '' }
      $problems += "Google Drive backup last succeeded $age min ago ($($last.ToString('yyyy-MM-dd HH:mm')) IST)$why"
    }
  }
} catch {
  $problems += "backup status file unreadable: $($_.Exception.Message)"
}

if ($problems) {
  Send "$ping/fail" ("VPS PROBLEM`n" + ($problems -join "`n"))
} else {
  Send $ping 'ok: dashboard up, backup fresh'
}
