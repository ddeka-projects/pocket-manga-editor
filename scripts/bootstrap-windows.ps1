#requires -Version 5.1
[CmdletBinding()]
param()
. (Join-Path $PSScriptRoot "windows-common.ps1")
$python = Join-Path $PocketRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $launcher = Get-Command "py.exe" -CommandType Application -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        Invoke-PocketChecked $launcher.Source @("-3", "-m", "venv", (Join-Path $PocketRoot ".venv"))
    }
    else {
        $launcher = Get-Command "python.exe" -CommandType Application -ErrorAction SilentlyContinue
        if ($null -eq $launcher) { throw "Install Python 3.11 or newer, then run this script again." }
        Invoke-PocketChecked $launcher.Source @("-m", "venv", (Join-Path $PocketRoot ".venv"))
    }
}
Invoke-PocketChecked $python @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 'Python 3.11 or newer is required.')")
Invoke-PocketChecked $python @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $PocketRoot "requirements.txt"))
$environmentFile = Join-Path $PocketRoot ".env"
if (-not (Test-Path -LiteralPath $environmentFile)) {
    Copy-Item -LiteralPath (Join-Path $PocketRoot ".env.example") -Destination $environmentFile
    Write-Host "Created .env. Review its paths and settings before starting."
}
else { Write-Host "Kept the existing .env file." }
Write-Host "Bootstrap complete: $PocketRoot"
