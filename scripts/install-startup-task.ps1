#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$TaskName = "Pocket Manga Editor Server"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from PowerShell as Administrator."
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$virtualPython = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$environmentFile = Join-Path $repositoryRoot ".env"
$runScript = Join-Path $PSScriptRoot "run-server.ps1"
$firewallRuleName = "Pocket Manga Editor Server (Private LAN)"

if (-not (Test-Path -LiteralPath $virtualPython -PathType Leaf)) {
    throw "The virtual environment is missing. Run scripts\bootstrap-windows.ps1 first."
}
if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    throw ".env is missing. Run scripts\bootstrap-windows.ps1, then configure the library path."
}

Push-Location -LiteralPath $repositoryRoot
try {
    $configuredPort = & $virtualPython -c (
        "from pocket_manga_editor.config import load_configuration; print(load_configuration().port)"
    )
    if ($LASTEXITCODE -ne 0) {
        throw "The Pocket Manga Editor configuration is invalid. Correct .env before installing startup."
    }
}
finally {
    Pop-Location
}
$configuredPort = [int](($configuredPort | Select-Object -Last 1).Trim())

$powerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$actionArguments = (
    '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $runScript
)
$action = New-ScheduledTaskAction `
    -Execute $powerShell `
    -Argument $actionArguments `
    -WorkingDirectory $repositoryRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$taskPrincipal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $taskPrincipal `
    -Description "Runs the Pocket Manga Editor local web server whenever Windows starts." `
    -Force | Out-Null

Get-NetFirewallRule -DisplayName $firewallRuleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue
New-NetFirewallRule `
    -DisplayName $firewallRuleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $configuredPort `
    -Profile Private `
    -RemoteAddress LocalSubnet | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host "Installed and started '$TaskName'." -ForegroundColor Green
Write-Host "Allowed TCP port $configuredPort only from the local subnet on Private networks." -ForegroundColor Green
Write-Host "Server output is written under $repositoryRoot\logs." -ForegroundColor Green
