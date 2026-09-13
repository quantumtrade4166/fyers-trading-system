# run_detached.ps1 — start a backtest on the VPS that outlives the SSH session.
#
# A long python started directly through ssh dies (or never starts) when the
# session closes: on 2026-09-13 two backtests launched that way produced no output
# and no process ever appeared. Start-Process detaches it, and the output goes to a
# file that can be polled instead of a pipe that disappears.
#
#   powershell -ExecutionPolicy Bypass -File run_detached.ps1 -Name perday -Args "per_day.py --profile ist_day"
param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Args
)
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$py   = Join-Path $root ".venv\Scripts\python.exe"
$bt   = Join-Path $root "live_trading_options\delta_btc\backtest"
$out  = Join-Path $bt "$Name.out.txt"
$err  = Join-Path $bt "$Name.err.txt"
Remove-Item $out, $err -ErrorAction SilentlyContinue

$parts = $Args.Split(" ", 2)
$script = Join-Path $bt $parts[0]
$rest = if ($parts.Count -gt 1) { $parts[1] } else { "" }

$p = Start-Process -FilePath $py -ArgumentList "-u `"$script`" $rest" `
     -WorkingDirectory $root -RedirectStandardOutput $out `
     -RedirectStandardError $err -WindowStyle Hidden -PassThru
Write-Host "started $Name pid $($p.Id) -> $out"
