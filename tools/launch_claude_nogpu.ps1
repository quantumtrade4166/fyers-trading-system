# launch_claude_nogpu.ps1 -- Start Claude Desktop (Store/MSIX install) with GPU acceleration off.
# The GT 610 is shared with DWM + EpicPen and runs ~74% busy; --disable-gpu makes Claude
# draw its UI on the CPU instead. Used by the Desktop shortcut "Claude (No GPU)".
#
# Claude is single-instance: if any Claude.exe is still alive (tray quit takes a few seconds
# to finish), a new launch just hands off to it and the flag is lost. So wait for it to exit.
# Each run is logged to tools\launch_claude_nogpu.log.

$log = Join-Path $PSScriptRoot 'launch_claude_nogpu.log'
function Log($m) { Add-Content $log ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }

try {
    $pkg = Get-AppxPackage -Name Claude | Select-Object -First 1
    if (-not $pkg) { throw "Claude Store package not found" }
    $exe = Join-Path $pkg.InstallLocation 'app\Claude.exe'

    # Desktop app processes only (the claude-code CLI is a different Claude.exe under AppData).
    $running = { @(Get-CimInstance Win32_Process -Filter "Name='Claude.exe'" |
                   Where-Object { $_.ExecutablePath -like "$($pkg.InstallLocation)*" }) }
    for ($i = 0; $i -lt 30 -and (& $running).Count -gt 0; $i++) { Start-Sleep 1 }
    if ((& $running).Count -gt 0) {
        Log "ABORT: Claude still running after 30s -- quit it from the tray first"
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show("Claude is still running.`nQuit it from the system tray (right-click > Quit), then use this shortcut again.", "Claude (No GPU)") | Out-Null
        exit 1
    }

    Invoke-CommandInDesktopPackage -PackageFamilyName $pkg.PackageFamilyName `
        -AppId Claude -Command $exe -Args '--disable-gpu' -ErrorAction Stop
    Log "Launched $exe --disable-gpu"
} catch {
    Log "ERROR: $($_.Exception.Message)"
    exit 1
}
