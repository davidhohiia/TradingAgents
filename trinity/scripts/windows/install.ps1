# TRINITY installer for Windows 10/11 (PowerShell).
# Run from the repository root:
#   powershell -ExecutionPolicy Bypass -File trinity\scripts\windows\install.ps1
$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\..\..\.."
$Trinity = Join-Path $Root "trinity"
Set-Location $Root

if (Get-Command py -ErrorAction SilentlyContinue) { $base = "py"; $baseArgs = @("-3.12") }
else { $base = "python"; $baseArgs = @() }
& $base @baseArgs -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
& $base @baseArgs -m venv "$Trinity\.venv"

$Py = "$Trinity\.venv\Scripts\python.exe"
& $Py -m pip install --upgrade pip
& $Py -m pip install -r "$Trinity\requirements.txt"

if (-not (Test-Path "$Trinity\.env")) {
    Copy-Item "$Trinity\.env.example" "$Trinity\.env"
    # restrict the .env file to the current user only
    icacls "$Trinity\.env" /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    Write-Host "Created trinity\.env - edit it with Notepad before starting."
}
& $Py "$Trinity\main.py" --init-db
& $Py "$Trinity\main.py" --check
Push-Location $Trinity
& $Py -m pytest -q
Pop-Location
Write-Host "Done. Start paper trading: trinity\scripts\windows\start_paper.bat"
