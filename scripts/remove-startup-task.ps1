#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$TaskName = "Pocket Manga Editor"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from PowerShell as Administrator."
}

$taskNames = @($TaskName)
if ($TaskName -eq "Pocket Manga Editor") {
    $taskNames += "Pocket Manga Editor Server"
}
foreach ($name in $taskNames) {
    $task = Get-ScheduledTask -TaskName $name -TaskPath "\" -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Stop-ScheduledTask -TaskName $name -TaskPath "\"
        Unregister-ScheduledTask -TaskName $name -TaskPath "\" -Confirm:$false
        Write-Host "Removed '$name'." -ForegroundColor Green
    }
}

foreach ($name in @("Pocket Manga Editor (Private LAN)", "Pocket Manga Editor Server (Private LAN)")) {
    Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule
}
Write-Host "Removed the Pocket Manga Editor firewall rule." -ForegroundColor Green
Write-Host "The library, .env, virtual environment, metadata, output, and logs were not deleted."
