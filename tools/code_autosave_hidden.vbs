' Launches code_autosave.ps1 completely hidden (no PowerShell window).
Set s = CreateObject("WScript.Shell")
s.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""G:\fyers_data_pipeline\tools\code_autosave.ps1""", 0, False
