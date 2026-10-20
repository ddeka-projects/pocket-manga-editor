#requires -Version 5.1
[CmdletBinding()]
param([string]$TaskName = "", [switch]$Pause, [switch]$Elevated)
$ErrorActionPreference = "Stop"
$exitCode = 1
$delegated = $false
try {
    . (Join-Path $PSScriptRoot "windows-common.ps1")
    if (-not $TaskName) { $TaskName = $PocketAppName }
    Assert-PocketTaskName $TaskName
    $task = Get-PocketTask $TaskName
    if ($null -eq $task) { throw "Scheduled task '$TaskName' was not found. Install it with scripts\install-startup-task.ps1 first." }
    if ([string]$task.State -eq "Disabled") { throw "Scheduled task '$TaskName' is disabled. Enable it before restarting." }
    $settings = Get-PocketSettings
    $previousHealth = Get-PocketHealth $settings
    $previousInstance = if ($null -ne $previousHealth) { [string]$previousHealth.instance_id } else { "" }
    Stop-PocketTask $TaskName
    Write-Host "Starting '$TaskName'..."
    Start-ScheduledTask -TaskName $TaskName -TaskPath "\" -ErrorAction Stop
    Wait-PocketServer -TaskName $TaskName -Settings $settings -PreviousInstance $previousInstance
    Write-Host "Restart complete. '$TaskName' is running and the web app is ready." -ForegroundColor Green
    $exitCode = 0
}
catch {
    if ((Get-Command Test-PocketAccessDenied -ErrorAction SilentlyContinue) -and (Test-PocketAccessDenied $_) -and -not $Elevated -and -not (Test-PocketAdministrator)) {
        Write-Host "Windows requires permission to manage this task. Requesting administrator access..."
        try {
            $powerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
            $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}" -TaskName "{1}" -Elevated' -f $PSCommandPath, $TaskName
            if ($Pause) { $arguments += " -Pause" }
            $process = Start-Process -FilePath $powerShell -ArgumentList $arguments -Verb RunAs -WorkingDirectory $PSScriptRoot -Wait -PassThru -ErrorAction Stop
            $delegated = $true
            $exitCode = $process.ExitCode
        }
        catch {
            if (Test-PocketCancelled $_) { Write-Host "Administrator request cancelled. Restart was not completed." -ForegroundColor Yellow }
            else { Write-Host "Could not obtain administrator access: $($_.Exception.Message)" -ForegroundColor Red }
        }
    }
    else { Write-Host "Restart failed: $($_.Exception.Message)" -ForegroundColor Red }
    Write-Host "Log: $(Join-Path $PSScriptRoot '..\logs\server.log')"
}
finally {
    if ($Pause -and -not $delegated) { [void](Read-Host "Press Enter to close") }
}
exit $exitCode
