# vps_step1_ssh.ps1 -- run ONCE on the new VPS in an Administrator PowerShell.
# Turns on SSH (key-only, home IP only) so Claude can do the rest of the rebuild.
$ErrorActionPreference = 'Stop'
$homeIp = '59.102.111.62'
$pub = 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQCvQmyzmd7+Ub1v01JfQbIzdwSE3ze6XFIAfIJ2QX5lc3g2NN2FqseexX0TuIQAwlRxv/7RL5JPx8Vie5P12vkTJJPsmr/HjT8/cJ16uvrH+KeJacmPsVzTuB+BDGmUmRgWUHXjslVlFhKmpUqHv0u/9ExwhSTku8zvJvnTISuuHWF1XVnk0NLMliamVKH/JrcXBJC5gJDvw2WUjPiFIILoekmjsbxiB6rkDV+u7ShuyYutv0fwt7ZTMBjJdczacepwJOrADOrQQ8Abp9q9jAtocb+n3tUeMKpFCpHQrSXrkxftfJQU9+hJDIwNvhowX+7vwlHl5XbFFFSz4+7yHt/6/0Ukm9ja77IXc7NgK1IZ7YrM4hvjieEr9HWW3RZq3CmrQ9k734KJR8HHezaLG8C6P2aDqyBKpx632y57HIE9dqdW/xxS1wHAlje0c3i0FRgnIYz81m2MRlnFDFGtZ2TLTUKQsFE6uI1VdcIYZM+nAhKYc0Sfif6FPfrts5Dzze9syq/vRwu22cUg99prRl9v9Xry4zpsXpO5nSsQFizgL3ao2/xvi0l6r7EWosleV3eLbza6dhgQBUy1fz17Tn0pcEqoEwCNj/CDbEM8j5zY+U3MkBzNqqbnWITP8utvIfvzbjfmYGBbHGAhg9pshMszM3h3b4L3vOcfY5s52qdi1Q== pc@DESKTOP-SJ5TFLG'

Write-Host '1/4 installing OpenSSH server (can take a few minutes)...'
if ((Get-WindowsCapability -Online -Name OpenSSH.Server*).State -ne 'Installed') {
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
}
Set-Service sshd -StartupType Automatic
Start-Service sshd

Write-Host '2/4 adding Claude key...'
$ak = 'C:\ProgramData\ssh\administrators_authorized_keys'
Set-Content -Path $ak -Value $pub -Encoding ascii
icacls $ak /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null

Write-Host '3/4 keys only, no passwords over SSH...'
$cfg = 'C:\ProgramData\ssh\sshd_config'
$c = Get-Content $cfg
$c = $c -replace '^#?\s*PasswordAuthentication.*', 'PasswordAuthentication no'
if (-not ($c -match '^PasswordAuthentication')) { $c += 'PasswordAuthentication no' }
Set-Content $cfg $c -Encoding ascii
Restart-Service sshd

Write-Host '4/4 firewall: port 22 open ONLY to home IP...'
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -EA SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH (home IP only)' -Direction Inbound -Protocol TCP -LocalPort 22 -RemoteAddress $homeIp -Action Allow | Out-Null

Write-Host ''
Write-Host 'DONE - tell Claude: ssh is on' -ForegroundColor Green
