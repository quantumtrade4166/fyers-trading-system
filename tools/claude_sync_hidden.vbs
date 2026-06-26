' Launches claude_sync_once.ps1 completely hidden (no PowerShell window flash).
' Used by the scheduled task "ClaudeUniverseLiveSync" so the every-minute run is invisible.
Set s = CreateObject("WScript.Shell")
s.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""G:\fyers_data_pipeline\tools\claude_sync_once.ps1""", 0, False
