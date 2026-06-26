' Launches vault_backup.ps1 completely hidden (no PowerShell window flash).
' Used by the scheduled task "ObsidianVaultLiveSync" so the every-2-min run is invisible.
Set s = CreateObject("WScript.Shell")
s.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""G:\fyers_data_pipeline\tools\vault_backup.ps1""", 0, False
