#requires -Version 5.1
[CmdletBinding()]
param([switch]$Reload)
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "run-server.ps1") -Reload:$Reload
exit $LASTEXITCODE
