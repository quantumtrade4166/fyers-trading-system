# backup_health.ps1 -- checks every automatic backup is fresh and pops a Windows
# notification if one has stopped. Run every 2 hours by scheduled task 'BackupHealthCheck'.
# Writes D:\My Drive\PC Backup\backup_health.json (JARVIS will read this later).

$ErrorActionPreference = 'Continue'
$drive = 'D:\My Drive'
function AgeHours($path) { if ($path -and (Test-Path $path)) { ((Get-Date) - (Get-Item $path).LastWriteTime).TotalHours } else { [double]::PositiveInfinity } }
function StatusAge($json) {
    if (-not (Test-Path $json)) { return [double]::PositiveInfinity }
    try { $s = Get-Content $json -Raw | ConvertFrom-Json; if ($s.last_ok) { return ((Get-Date) - [datetime]$s.last_ok).TotalHours } } catch { }
    [double]::PositiveInfinity
}

# Vault: how far the backup lags behind the newest change in the vault (0 = up to date).
$vaultNewest = (Get-ChildItem 'G:\Trading Brain' -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notmatch '\\\.obsidian\\|\\\.trash\\' } | Sort-Object LastWriteTime | Select-Object -Last 1).LastWriteTime
$backupNewest = (Get-ChildItem "$drive\Obsidian Vault backup" -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'vault_backup_log.txt' -and $_.FullName -notmatch '\\\.obsidian\\|\\\.trash\\' } | Sort-Object LastWriteTime | Select-Object -Last 1).LastWriteTime
$vaultLag = if ($vaultNewest -and $backupNewest) { [math]::Max(0, ($vaultNewest - $backupNewest).TotalHours) } else { [double]::PositiveInfinity }

$checks = @(
    @{ name = 'PC project + JARVIS -> Google Drive'; age = StatusAge "$drive\PC Backup\pc_backup_status.json"; max = 2 },
    @{ name = 'Code -> GitHub';                      age = StatusAge "$drive\PC Backup\code_autosave_status.json"; max = 30 },
    @{ name = 'VPS -> Google Drive';                 age = AgeHours "$drive\VPS Backup\vps_backup_log.txt"; max = 1 },
    @{ name = 'JARVIS database backups';             age = AgeHours ((Get-ChildItem "$drive\JARVIS_backups" -Filter *.jvbak -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1).FullName); max = 30 },
    @{ name = 'Obsidian vault -> Google Drive';      age = $vaultLag; max = 1 }
)
$stale = $checks | Where-Object { $_.age -gt $_.max }
$report = @{ checked_at = (Get-Date).ToString('o'); ok = (-not $stale)
             checks = $checks | ForEach-Object { @{ name = $_.name; age_hours = [math]::Round([math]::Min($_.age, 99999), 1); max_hours = $_.max; ok = ($_.age -le $_.max) } } }
$report | ConvertTo-Json -Depth 4 | Set-Content "$drive\PC Backup\backup_health.json" -Encoding utf8

if ($stale) {
    $text = 'Backup problem: ' + (($stale | ForEach-Object { $_.name }) -join ', ')
    try {
        [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
        $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $nodes = $xml.GetElementsByTagName('text')
        [void]$nodes.Item(0).AppendChild($xml.CreateTextNode('Backup check'))
        [void]$nodes.Item(1).AppendChild($xml.CreateTextNode($text))
        $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Windows PowerShell').Show($toast)
    } catch { }
}
