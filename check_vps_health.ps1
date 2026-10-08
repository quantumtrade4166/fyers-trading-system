# Health check the DualMom service on the VPS itself
try {
  $r = Invoke-RestMethod -Uri "http://127.0.0.1:8010/api/dualmom/live/status" -TimeoutSec 20
  Write-Host "ok=$($r.ok)  enabled=$($r.config.ENABLED)  dry_run=$($r.config.DRY_RUN)"
  Write-Host "jobs: $($r.jobs | ForEach-Object { $_.id } | Sort-Object)"
} catch {
  Write-Host "FAILED: $($_.Exception.Message)"
}
