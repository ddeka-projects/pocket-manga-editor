#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$virtualPython = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$logDirectory = Join-Path $repositoryRoot "logs"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logFile = Join-Path $logDirectory ("server-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

if (-not (Test-Path -LiteralPath $virtualPython -PathType Leaf)) {
    Add-Content -LiteralPath $logFile -Encoding UTF8 -Value (
        "[{0}] Server was not started because .venv is missing. Run scripts\bootstrap-windows.ps1 first." -f (Get-Date -Format "o")
    )
    exit 1
}

Set-Location -LiteralPath $repositoryRoot
Add-Content -LiteralPath $logFile -Encoding UTF8 -Value (
    "[{0}] Starting Pocket Manga Editor." -f (Get-Date -Format "o")
)

& $virtualPython -u -m pocket_manga_editor >> $logFile 2>&1
$serverExitCode = $LASTEXITCODE

Add-Content -LiteralPath $logFile -Encoding UTF8 -Value (
    "[{0}] Pocket Manga Editor stopped with exit code {1}." -f (Get-Date -Format "o"), $serverExitCode
)
exit $serverExitCode
