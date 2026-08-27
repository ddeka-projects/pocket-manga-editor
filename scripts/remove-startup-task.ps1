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

$firewallRuleName = "Pocket Manga Editor Server (Private LAN)"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'." -ForegroundColor Green
}
else {
    Write-Host "The startup task was not installed." -ForegroundColor Yellow
}

Get-NetFirewallRule -DisplayName $firewallRuleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue
Write-Host "Removed the Pocket Manga Editor firewall rule." -ForegroundColor Green
Write-Host "The library, .env, virtual environment, metadata, output, and logs were not deleted."
