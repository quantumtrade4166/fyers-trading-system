# Stop ONLY the Breeze downloader — never anything else.
#
# This box runs live money: the VWAP strangle and the delta-neutral engine are
# both python processes, and killing one of those mid-session has caused a real
# outage before. So this never matches on "python.exe"; it matches on the
# command line containing the downloader module, and reports exactly what it
# killed.
#
# Called by the BreezeDownloadStop scheduled task at 09:00 on weekdays, so the
# download is out of the way before the market opens.

$pattern = 'options\.breeze\.downloader'
$killed = 0

Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'cmd.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern } |
    ForEach-Object {
        Write-Output ("stopping PID {0}: {1}" -f $_.ProcessId,
                      $_.CommandLine.Substring(0, [Math]::Min(110, $_.CommandLine.Length)))
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            $killed++
        } catch {
            Write-Output ("  could not stop {0}: {1}" -f $_.ProcessId, $_.Exception.Message)
        }
    }

# The wrapper .bat that launched it, if it is still sitting there
Get-CimInstance Win32_Process -Filter "Name = 'cmd.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'run_vps_download\.bat' } |
    ForEach-Object {
        Write-Output ("stopping wrapper PID {0}" -f $_.ProcessId)
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++ } catch {}
    }

if ($killed -eq 0) {
    Write-Output "No Breeze downloader running — nothing to stop."
} else {
    Write-Output ("Stopped {0} process(es). The download is resumable: the " -f $killed +
                  "manifest records every contract-day, so the next run picks up " +
                  "where this left off and re-spends no calls.")
}
