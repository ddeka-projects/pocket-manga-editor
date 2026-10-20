#requires -Version 5.1
[CmdletBinding()]
param([string]$TaskName = "", [switch]$ConfigureFirewall)
. (Join-Path $PSScriptRoot "windows-common.ps1")
if (-not $TaskName) { $TaskName = $PocketAppName }
Assert-PocketTaskName $TaskName
if ($ConfigureFirewall -and -not (Test-PocketAdministrator)) { throw "-ConfigureFirewall requires PowerShell as Administrator under the same Windows account. No changes were made." }
$pythonw = Get-PocketPython -Windowless
$runner = Join-Path $PSScriptRoot "run-server-background.pyw"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "Background launcher missing: $runner" }
$settings = Get-PocketSettings
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('-B "{0}"' -f $runner) -WorkingDirectory $PocketRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$previousHealth = Get-PocketHealth $settings
$previousInstance = if ($null -ne $previousHealth) { [string]$previousHealth.instance_id } else { "" }
Stop-PocketTask $TaskName
Register-ScheduledTask -TaskName $TaskName -TaskPath "\" -Action $action -Trigger $trigger -Principal $principal -Settings $taskSettings -Description "Runs $PocketAppName when $($identity.Name) signs in." -Force | Out-Null
if ($ConfigureFirewall) { Set-PocketFirewall -Port $settings.port }
Start-ScheduledTask -TaskName $TaskName -TaskPath "\" -ErrorAction Stop
Wait-PocketServer -TaskName $TaskName -Settings $settings -PreviousInstance $previousInstance
Write-Host "Installed and started '$TaskName' at user logon with limited privileges." -ForegroundColor Green
Write-Host "Log: $(Join-Path $PocketRoot 'logs\server.log')"
if (-not $ConfigureFirewall) { Write-Host "Firewall unchanged. To configure Private/LocalSubnet access, run elevated with -ConfigureFirewall." }
