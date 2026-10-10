# vps_restore_market_data.ps1 -- copies the research / market datasets from Google Drive onto
# the VPS (2026-10-11 rebuild). Runs ON the VPS as Administrator (G: = Google Drive is per-user).
#
# Source: the PC's whole-folder mirror in Drive (G:\My Drive\PC Backup\fyers_data_pipeline)
# plus the Drive-root datasets (Dukascopy_1m, XAUUSD_1s_Data).
#
# Safety: NEVER copies live state onto the VPS. The VPS is the source of truth for anything a
# running engine or the dashboard writes (deployment\, live_trading_options\ state, strangle
# flags, logs, config, .env), and an old PC copy of a live_control_*.json could ARM a strategy.
# /XO: an older file never overwrites a newer one already on the VPS.
$ErrorActionPreference = 'Continue'
$repo = 'C:\trading\fyers_data_pipeline'
$pc = 'G:\My Drive\PC Backup\fyers_data_pipeline'
$log = 'C:\trading\ops\logs\market_data_restore.log'
function Log($m) { $l = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m; Add-Content $log $l; $l }
$rc = @('/E', '/XO', '/COPY:DAT', '/R:3', '/W:10', '/MT:16', '/NFL', '/NDL', '/NP', '/NJH', '/NJS')
$noState = @('/XF', '*.pyc', '.env', 'live_control*.json', '*_resume.json', 'TICK.json',
             '/XD', 'live_state', 'vwap_state', '__pycache__', 'node_modules', '.tmp.driveupload', '.tmp.drivedownload')

Log 'start'
# whole datasets
foreach ($d in 'data', 'Bitcoin options data', 'Bhavcopy', 'Nifty fno 2021-26', 'Nifty FNO List', 'backtesting', 'forex', 'crypto', 'options', 'etf', 'storage', 'tracker') {
    if (-not (Test-Path "$pc\$d")) { continue }
    & robocopy "$pc\$d" "$repo\$d" @rc @noState | Out-Null
    Log ("{0,-24} rc={1} files now={2}" -f $d, $LASTEXITCODE, (Get-ChildItem "$repo\$d" -Recurse -File -EA SilentlyContinue).Count)
}
# research data inside live_trading_options -- NOT the engines' state folders
foreach ($d in 'delta_btc\data\chain_archive', 'delta_btc\backtest') {
    if (-not (Test-Path "$pc\live_trading_options\$d")) { continue }
    & robocopy "$pc\live_trading_options\$d" "$repo\live_trading_options\$d" @rc @noState | Out-Null
    Log ("{0,-24} rc={1}" -f $d, $LASTEXITCODE)
}
# Drive-root datasets
& robocopy 'G:\My Drive\Dukascopy_1m' "$repo\forex\Dukascopy_1m" @rc | Out-Null
Log ("Dukascopy_1m (11 instruments) rc={0} files now={1}" -f $LASTEXITCODE, (Get-ChildItem "$repo\forex\Dukascopy_1m" -Recurse -File).Count)
& robocopy 'G:\My Drive\XAUUSD_1s_Data' "$repo\forex\XAUUSD_1s_Data" @rc | Out-Null
Log ("XAUUSD_1s_Data rc={0} files now={1}" -f $LASTEXITCODE, (Get-ChildItem "$repo\forex\XAUUSD_1s_Data" -Recurse -File).Count)
Log 'done'
# the PC's BTC history days are now on disk -> the downloader only fetches what is missing
Start-ScheduledTask -TaskName 'BTCHistoryDownloadStart' -EA SilentlyContinue
Log 'BTC history download kicked (it runs only in the 17:20-09:00 IST window)'
