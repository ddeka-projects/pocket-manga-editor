#requires -Version 5.1
[CmdletBinding()]
param([string]$TaskName = "", [switch]$RemoveFirewall)
. (Join-Path $PSScriptRoot "windows-common.ps1")
if (-not $TaskName) { $TaskName = $PocketAppName }
Assert-PocketTaskName $TaskName
if ($RemoveFirewall -and -not (Test-PocketAdministrator)) { throw "-RemoveFirewall requires PowerShell as Administrator under the same Windows account. No changes were made." }
$task = Get-PocketTask $TaskName
if ($null -ne $task) {
    Stop-PocketTask $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath "\" -Confirm:$false -ErrorAction Stop
    Write-Host "Stopped and removed '$TaskName'."
}
else { Write-Host "Scheduled task not found: $TaskName" }
if ($RemoveFirewall) {
    Get-NetFirewallRule -DisplayName "$PocketAppName (Private LAN)" -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction Stop
    Write-Host "Removed this app's firewall rule."
}
else { Write-Host "Firewall unchanged. To also remove its rule, run elevated with -RemoveFirewall." }
Write-Host "Configuration, application data, media, and backups were retained."
