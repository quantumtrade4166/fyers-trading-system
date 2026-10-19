# Stop ONLY the MCX terminal engine (python.exe whose command line runs mcx_terminal.server). Kill by PID.
$p = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*mcx_terminal.server*' }
if (-not $p) { 'mcx terminal: not running'; exit 0 }
$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "stopped PID $($_.ProcessId)" }
