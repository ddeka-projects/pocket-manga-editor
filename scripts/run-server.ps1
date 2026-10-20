#requires -Version 5.1
[CmdletBinding()]
param([switch]$Reload)
. (Join-Path $PSScriptRoot "windows-common.ps1")
if ($Reload -and -not $PocketSupportsReload) { throw "Automatic code reload is not available for this app. Restart the foreground server after code changes." }
$python = Get-PocketPython
$arguments = @("-u", "-B", "-m", $PocketPackage)
if ($Reload) { $arguments += "--reload" }
Push-Location -LiteralPath $PocketRoot
try {
    $ErrorActionPreference = "Continue"
    & $python @arguments
    $exitCode = $LASTEXITCODE
}
finally { Pop-Location }
exit $exitCode
