# btc_history_window.ps1 -- BTC options history download, NIGHT WINDOW ONLY.
#   -Start  (task BTCHistoryDownloadStart, 17:20 IST daily) runs the resumable downloader
#   -Stop   (task BTCHistoryDownloadStop,  09:00 IST daily) stops it before the live session
# The downloader shares this VPS's IP with the LIVE BTC VWAP engine (09:30-17:10 IST); a bulk
# download in that window could hit Delta's rate limit and slow the live engine's polls.
# Resumable: days already on disk are skipped, so it simply carries on each night until done.
param([switch]$Start, [switch]$Stop)
$repo = 'C:\trading\fyers_data_pipeline'
$log = "$repo\Bitcoin options data\download_vps.log"
$match = 'download_delta_btc_options\.py'
$running = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match $match }
if ($Stop) {
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
    Add-Content $log ("{0}  window closed - stopped {1} process(es)" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), @($running).Count)
    exit 0
}
$now = Get-Date
if ($now.Hour -ge 9 -and ($now.Hour -lt 17 -or ($now.Hour -eq 17 -and $now.Minute -lt 15))) { exit 0 }   # never in the live session
if ($running) { exit 0 }
Add-Content $log ("{0}  window open - starting downloader" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Set-Location $repo
& "$repo\.venv\Scripts\python.exe" -u "$repo\Bitcoin options data\download_delta_btc_options.py" >> $log 2>&1
