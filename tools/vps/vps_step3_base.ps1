# vps_step3_base.ps1 -- Python 3.12 + Git + code + venv on the VPS (everything under C:\trading).
$ErrorActionPreference = 'Continue'
$dl = "$env:USERPROFILE\vps_setup\dl"; New-Item -ItemType Directory -Force $dl | Out-Null
$log = 'C:\trading\ops\logs\setup.log'
function Log($m) { $l = "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $m; $l; Add-Content $log $l }

if (-not (Test-Path 'C:\Python312\python.exe')) {
    Log 'downloading Python 3.12.10'
    curl.exe -sL -o "$dl\py.exe" https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
    Start-Process "$dl\py.exe" -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 TargetDir=C:\Python312' -Wait
}
Log ('python: ' + (& C:\Python312\python.exe --version 2>&1))

if (-not (Test-Path 'C:\Program Files\Git\cmd\git.exe')) {
    Log 'downloading Git for Windows'
    $rel = Invoke-RestMethod https://api.github.com/repos/git-for-windows/git/releases/latest -UseBasicParsing
    $url = ($rel.assets | Where-Object { $_.name -match '^Git-[\d.]+-64-bit\.exe$' } | Select-Object -First 1).browser_download_url
    curl.exe -sL -o "$dl\git.exe" $url
    Start-Process "$dl\git.exe" -ArgumentList '/VERYSILENT /NORESTART /NOCANCEL /SP- /SUPPRESSMSGBOXES' -Wait
}
Log ('git: ' + (& 'C:\Program Files\Git\cmd\git.exe' --version 2>&1))

$repo = 'C:\trading\fyers_data_pipeline'
if (-not (Test-Path "$repo\deployment")) {
    Log 'unpacking code'
    Expand-Archive "$env:USERPROFILE\vps_setup\code.zip" $repo -Force
}
Log ('code files: ' + (Get-ChildItem $repo -Recurse -File | Measure-Object).Count)

if (-not (Test-Path "$repo\.venv\Scripts\python.exe")) { & C:\Python312\python.exe -m venv "$repo\.venv" }
Log 'installing packages (several minutes)'
& "$repo\.venv\Scripts\python.exe" -m pip install -q --upgrade pip 2>&1 | Out-Null
$fails = @()
& "$repo\.venv\Scripts\python.exe" -m pip install -q -r "$env:USERPROFILE\vps_setup\requirements_vps.txt" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Log 'bulk install failed - installing one by one'
    foreach ($r in Get-Content "$env:USERPROFILE\vps_setup\requirements_vps.txt") {
        & "$repo\.venv\Scripts\python.exe" -m pip install -q $r 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { $fails += $r }
    }
}
Log ('packages installed: ' + (& "$repo\.venv\Scripts\python.exe" -m pip freeze 2>$null | Measure-Object).Count)
if ($fails) { Log ('FAILED: ' + ($fails -join ', ')) }
Log 'step3 done'

# Fresh Windows Server has an incomplete root store -> CERTIFICATE_VERIFY_FAILED for Delta /
# websockets. sitecustomize.py points every venv Python at certifi's bundle.
Copy-Item "$repo\tools\vps\sitecustomize.py" "$repo\.venv\Lib\site-packages\sitecustomize.py" -Force
Log 'sitecustomize (certifi CA bundle) installed'
