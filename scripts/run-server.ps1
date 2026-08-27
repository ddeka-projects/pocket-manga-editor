#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$virtualPython = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$logDirectory = Join-Path $repositoryRoot "logs"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logStamp = Get-Date -Format "yyyy-MM-dd-HHmmss-fff"
$logFile = Join-Path $logDirectory ("server-{0}.log" -f $logStamp)
$standardOutputLog = Join-Path $logDirectory ("server-{0}.stdout.log" -f $logStamp)

if (-not (Test-Path -LiteralPath $virtualPython -PathType Leaf)) {
    Add-Content -LiteralPath $logFile -Encoding UTF8 -Value (
        "[{0}] Server was not started because .venv is missing. Run scripts\bootstrap-windows.ps1 first." -f (Get-Date -Format "o")
    )
    exit 1
}

Set-Location -LiteralPath $repositoryRoot

# Windows PowerShell 5.1 turns native stderr lines into PowerShell ErrorRecord
# objects.  Invoking Python with ``& ... 2>&1`` while ErrorActionPreference is
# Stop therefore misclassifies ordinary logging as a terminating
# NativeCommandError.  Start-Process redirects the native streams directly to
# files, without asking PowerShell to interpret them.
$serverProcess = Start-Process `
    -FilePath $virtualPython `
    -ArgumentList @("-u", "-m", "pocket_manga_editor") `
    -WorkingDirectory $repositoryRoot `
    -NoNewWindow `
    -RedirectStandardOutput $standardOutputLog `
    -RedirectStandardError $logFile `
    -PassThru
$serverProcess.WaitForExit()
$serverExitCode = $serverProcess.ExitCode

Add-Content -LiteralPath $logFile -Encoding UTF8 -Value (
    "[{0}] Pocket Manga Editor stopped with exit code {1}." -f (Get-Date -Format "o"), $serverExitCode
)
exit $serverExitCode
