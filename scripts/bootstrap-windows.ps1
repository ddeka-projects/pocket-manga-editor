#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$virtualEnvironment = Join-Path $repositoryRoot ".venv"
$virtualPython = Join-Path $virtualEnvironment "Scripts\python.exe"
$requirements = Join-Path $repositoryRoot "requirements.txt"
$environmentFile = Join-Path $repositoryRoot ".env"
$environmentExample = Join-Path $repositoryRoot ".env.example"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

if (-not (Test-Path -LiteralPath $virtualPython -PathType Leaf)) {
    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        Write-Host "Creating .venv with the Windows Python launcher..." -ForegroundColor Cyan
        Invoke-Checked -FilePath $launcher.Source -Arguments @("-3", "-m", "venv", $virtualEnvironment)
    }
    else {
        $launcher = Get-Command "python.exe" -ErrorAction SilentlyContinue
        if ($null -eq $launcher) {
            throw "Python 3 was not found. Install Python 3.10 or newer, then run this script again."
        }
        Write-Host "Creating .venv with python.exe..." -ForegroundColor Cyan
        Invoke-Checked -FilePath $launcher.Source -Arguments @("-m", "venv", $virtualEnvironment)
    }
}

Invoke-Checked -FilePath $virtualPython -Arguments @(
    "-c",
    "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 'Pocket Manga Editor requires Python 3.10 or newer.')"
)

Write-Host "Installing the current runtime requirements..." -ForegroundColor Cyan
Invoke-Checked -FilePath $virtualPython -Arguments @(
    "-m", "pip", "install", "--disable-pip-version-check", "-r", $requirements
)

if (-not (Test-Path -LiteralPath $environmentFile)) {
    Copy-Item -LiteralPath $environmentExample -Destination $environmentFile
    Write-Host "Created .env from .env.example." -ForegroundColor Yellow
    Write-Host "Edit $environmentFile and set POCKET_MANGA_EDITOR_WORKING_DIRECTORY before starting the server." -ForegroundColor Yellow
}
else {
    Write-Host "Kept the existing .env file." -ForegroundColor Green
}

Write-Host "Windows bootstrap complete." -ForegroundColor Green
