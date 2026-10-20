#requires -Version 5.1
# Run only against a disposable fixture; all OS changes are replaced below.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$FixtureRoot,
      [Parameter(Mandatory=$true)][string]$Scenario,
      [Parameter(Mandatory=$true)][string]$PythonExecutable)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$global:PocketTestEvents = New-Object 'System.Collections.Generic.List[string]'
$global:PocketTestState = @{
    Exists = $true; State = "Running"; PendingReads = 0; Started = $false;
    Admin = ($Scenario -in @("install-firewall", "remove-firewall")); Scenario = $Scenario
}
$global:PocketTestPython = $PythonExecutable
$env:SystemRoot = $FixtureRoot

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}
function Add-Event([string]$Value) { $global:PocketTestEvents.Add($Value) }
function Assert-Task($TaskName, $TaskPath) {
    Assert-True ($TaskName -eq $PocketAppName) "Wrong task name: $TaskName"
    Assert-True ($TaskPath -eq "\") "Wrong task path: $TaskPath"
}
function global:Get-ScheduledTask {
    [CmdletBinding()] param($TaskName, $TaskPath)
    Assert-Task $TaskName $TaskPath
    Add-Event "query"
    if ($global:PocketTestState.Scenario -in @("restart-access", "restart-cancel", "restart-elevated")) {
        throw [UnauthorizedAccessException]::new("Task access denied")
    }
    if (-not $global:PocketTestState.Exists) {
        $record = [Management.Automation.ErrorRecord]::new([Exception]::new("Missing task"), "MissingTask", [Management.Automation.ErrorCategory]::ObjectNotFound, $TaskName)
        $PSCmdlet.ThrowTerminatingError($record)
    }
    if ($global:PocketTestState.PendingReads -gt 0) {
        $global:PocketTestState.PendingReads--
        if ($global:PocketTestState.PendingReads -eq 0) { $global:PocketTestState.State = "Ready" }
    }
    return [pscustomobject]@{ State = $global:PocketTestState.State }
}
function global:Stop-ScheduledTask {
    [CmdletBinding()] param($TaskName, $TaskPath)
    Assert-Task $TaskName $TaskPath
    Add-Event "stop"
    if ($Scenario -ne "restart-stop-timeout") { $global:PocketTestState.PendingReads = 2 }
}
function global:Start-ScheduledTask {
    [CmdletBinding()] param($TaskName, $TaskPath)
    Assert-Task $TaskName $TaskPath
    Assert-True ($global:PocketTestState.State -eq "Ready") "Started before stop was confirmed"
    Add-Event "start"
    $global:PocketTestState.State = "Running"
    $global:PocketTestState.Started = $true
}
function global:Unregister-ScheduledTask {
    [CmdletBinding(SupportsShouldProcess=$true)] param($TaskName, $TaskPath)
    Assert-Task $TaskName $TaskPath
    Assert-True ($global:PocketTestState.State -eq "Ready") "Removed before stop was confirmed"
    Add-Event "remove"
    $global:PocketTestState.Exists = $false
}
function global:Register-ScheduledTask {
    [CmdletBinding()] param($TaskName, $TaskPath, $Action, $Trigger, $Principal, $Settings, $Description, [switch]$Force)
    Assert-Task $TaskName $TaskPath
    Assert-True ($Action.Execute -like "*pythonw.exe") "Scheduled launch must use pythonw"
    Assert-True ($Action.Argument -like '*run-server-background.pyw*') "Wrong background launcher"
    Assert-True ($Principal.RunLevel -eq "Limited") "Server must have normal privileges"
    Assert-True ($Principal.LogonType -eq "Interactive") "Server must use the user's desktop session"
    Assert-True ($Settings.MultipleInstances -eq "IgnoreNew") "Duplicate task instances allowed"
    Assert-True ($Settings.ExecutionTimeLimit -eq [TimeSpan]::Zero) "Server task has a time limit"
    Add-Event "register"
    $global:PocketTestState.State = "Ready"
    return [pscustomobject]@{ TaskName = $TaskName }
}
function global:New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory) return [pscustomobject]@{ Execute=$Execute; Argument=$Argument; WorkingDirectory=$WorkingDirectory } }
function global:New-ScheduledTaskTrigger { param([switch]$AtLogOn, $User) return [pscustomobject]@{ User=$User } }
function global:New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel) return [pscustomobject]@{ UserId=$UserId; LogonType=$LogonType; RunLevel=$RunLevel } }
function global:New-ScheduledTaskSettingsSet {
    param($MultipleInstances, $RestartCount, $RestartInterval, [switch]$StartWhenAvailable,
          $ExecutionTimeLimit, [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries)
    return [pscustomobject]@{ MultipleInstances=$MultipleInstances; ExecutionTimeLimit=$ExecutionTimeLimit }
}
function global:Get-NetFirewallRule { [CmdletBinding()] param($DisplayName) Add-Event "firewall-query"; return [pscustomobject]@{ DisplayName=$DisplayName } }
function global:Remove-NetFirewallRule { [CmdletBinding()] param([Parameter(ValueFromPipeline=$true)]$InputObject) process { Add-Event "firewall-remove" } }
function global:New-NetFirewallRule {
    [CmdletBinding()] param($DisplayName,$Direction,$Action,$Protocol,$LocalPort,$Profile,$RemoteAddress)
    Assert-True ($Profile -eq "Private" -and $RemoteAddress -eq "LocalSubnet") "Firewall scope widened"
    Add-Event "firewall-create"
}
function global:Invoke-WebRequest {
    [CmdletBinding()] param($Uri, [switch]$UseBasicParsing, $TimeoutSec)
    $instance = if ($global:PocketTestState.Started) { "new-instance" } else { "old-instance" }
    if ($Scenario -eq "restart-old-health") { $instance = "old-instance" }
    $application = if ($Scenario -eq "restart-wrong-health") { "different_app" } else { $PocketPackage }
    $health = @{ ok=$true; app=$application; instance_id=$instance } | ConvertTo-Json -Compress
    Add-Event "health:$instance"
    return [pscustomobject]@{ StatusCode=200; Content=$health }
}
function global:Start-Sleep { param($Milliseconds) }
function global:Read-Host { param($Prompt) Add-Event "pause"; return "" }
function global:Start-Process {
    [CmdletBinding()] param($FilePath,$ArgumentList,$Verb,$WorkingDirectory,[switch]$Wait,[switch]$PassThru)
    Assert-True ($Verb -eq "RunAs" -and $Wait -and $PassThru) "Invalid UAC delegation"
    Assert-True ($ArgumentList -like '*-Elevated*') "UAC delegation lacks retry guard"
    Add-Event "uac"
    if ($Scenario -eq "restart-cancel") {
        throw [InvalidOperationException]::new("Elevation cancelled", [ComponentModel.Win32Exception]::new(1223))
    }
    return [pscustomobject]@{ ExitCode=0 }
}

# The fixture windows-common.ps1 invokes this after declaring production helpers.
# Keep the actual stop/wait, health, task lookup, error detection and native helper.
function global:Install-PocketTestAdapters {
    function global:Test-PocketAdministrator { return $global:PocketTestState.Admin }
    function global:Get-PocketPython {
        param([switch]$Windowless)
        if ($Windowless) { return (Join-Path $FixtureRoot ".venv\Scripts\pythonw.exe") }
        return $global:PocketTestPython
    }
    function global:Get-PocketSettings {
        if ($Scenario -eq "restart-config-error") { throw "Invalid test configuration" }
        return [pscustomobject]@{ host="0.0.0.0"; port=8787 }
    }
    if ($Scenario -eq "restart-stop-timeout") {
        $global:PocketTestStop = (Get-Command Stop-PocketTask).ScriptBlock
        function global:Stop-PocketTask { param($TaskName) & $global:PocketTestStop -TaskName $TaskName -TimeoutSeconds 0 }
    }
    if ($Scenario -in @("restart-old-health", "restart-wrong-health")) {
        $global:PocketTestWait = (Get-Command Wait-PocketServer).ScriptBlock
        function global:Wait-PocketServer { param($TaskName,$Settings,$PreviousInstance) & $global:PocketTestWait -TaskName $TaskName -Settings $Settings -PreviousInstance $PreviousInstance -TimeoutSeconds 0 }
    }
    if ($Scenario -like "bootstrap-*") {
        function global:Invoke-PocketChecked {
            param($FilePath, [string[]]$Arguments)
            Add-Event ("native:" + ($Arguments -join " "))
        }
    }
}

$failure = ""
$scriptExit = 0
if ($Scenario -eq "parse") {
    foreach ($script in Get-ChildItem -LiteralPath (Join-Path $FixtureRoot "scripts") -Filter *.ps1) {
        $tokens = $null; $errors = $null
        [void][Management.Automation.Language.Parser]::ParseFile($script.FullName, [ref]$tokens, [ref]$errors)
        Assert-True ($errors.Count -eq 0) "PowerShell syntax error in $($script.Name): $errors"
    }
}
elseif ($Scenario -eq "native") {
    . (Join-Path $FixtureRoot "scripts\windows-common.ps1")
    $result = @(Invoke-PocketChecked $PythonExecutable @("-c", "import sys; print('normal native stderr', file=sys.stderr); print('ok')"))
    Assert-True ($result[-1] -eq "ok") "Successful native command lost stdout"
    try { Invoke-PocketChecked $PythonExecutable @("-c", "raise SystemExit(7)") } catch { $failure = $_.Exception.Message }
    Assert-True ($failure -like '*exit code 7*') "Native failure exit code was ignored"
}
else {
    if ($Scenario -eq "restart-missing") { $global:PocketTestState.Exists = $false }
    if ($Scenario -eq "restart-disabled") { $global:PocketTestState.State = "Disabled" }
    $scriptName = "restart-server.ps1"
    $arguments = @{}
    if ($Scenario -like "install-*") {
        $scriptName = "install-startup-task.ps1"
        if ($Scenario -in @("install-firewall", "install-admin-gate")) { $arguments = @{ ConfigureFirewall=$true } }
    }
    elseif ($Scenario -like "remove-*") {
        $scriptName = "remove-startup-task.ps1"
        if ($Scenario -in @("remove-firewall", "remove-admin-gate")) { $arguments = @{ RemoveFirewall=$true } }
    }
    elseif ($Scenario -like "bootstrap-*") { $scriptName = "bootstrap-windows.ps1" }
    elseif ($Scenario -eq "restart-elevated") { $arguments = @{ Elevated=$true; Pause=$true } }
    else { $arguments = @{ Pause=$true } }
    try {
        & (Join-Path $FixtureRoot "scripts\$scriptName") @arguments
        if ($null -ne (Get-Variable LASTEXITCODE -ErrorAction SilentlyContinue)) { $scriptExit = $LASTEXITCODE }
    }
    catch { $failure = $_.Exception.Message; $scriptExit = 1 }
    $events = @($global:PocketTestEvents)
    switch ($Scenario) {
        "install-normal" {
            Assert-True ($scriptExit -eq 0 -and $events -contains "register" -and $events -contains "start") "Install failed: $failure"
            Assert-True (-not ($events -like "firewall-*")) "Ordinary install changed firewall"
        }
        "install-firewall" { Assert-True ($scriptExit -eq 0 -and $events -contains "firewall-create") "Explicit firewall install failed: $failure" }
        "remove-normal" {
            Assert-True ($scriptExit -eq 0 -and $events -contains "remove") "Remove failed: $failure"
            Assert-True (-not ($events -like "firewall-*")) "Ordinary remove changed firewall"
        }
        "remove-firewall" { Assert-True ($scriptExit -eq 0 -and $events -contains "firewall-remove") "Explicit firewall removal failed: $failure" }
        { $_ -in @("install-admin-gate", "remove-admin-gate") } {
            Assert-True ($scriptExit -ne 0 -and $failure -like '*Administrator*') "Elevation gate failed"
            Assert-True ($events.Count -eq 0) "Elevation gate allowed changes"
        }
        "restart-normal" {
            Assert-True ($scriptExit -eq 0 -and $events -contains "stop" -and $events -contains "start") "Restart failed: $failure"
            Assert-True ($events -contains "pause" -and $events -notcontains "uac") "Ordinary restart prompted UAC or did not pause"
            Assert-True ($events.IndexOf("stop") -lt $events.IndexOf("start")) "Restart order is wrong"
        }
        { $_ -in @("restart-missing", "restart-disabled", "restart-config-error", "restart-stop-timeout") } {
            Assert-True ($scriptExit -ne 0 -and $events -notcontains "start" -and $events -notcontains "uac") "Invalid restart started or prompted UAC"
            Assert-True ($events -contains "pause") "Failed restart did not pause"
            if ($Scenario -eq "restart-config-error") { Assert-True ($events -notcontains "stop") "Bad config stopped running server" }
        }
        { $_ -in @("restart-old-health", "restart-wrong-health") } {
            Assert-True ($scriptExit -ne 0 -and $events -contains "start" -and $events -notcontains "uac") "Invalid readiness was accepted"
        }
        "restart-access" {
            Assert-True ($scriptExit -eq 0 -and @($events | Where-Object { $_ -eq "uac" }).Count -eq 1) "Permission failure did not delegate once"
            Assert-True ($events -notcontains "pause") "Delegating parent should leave pause to child"
        }
        "restart-cancel" { Assert-True ($scriptExit -ne 0 -and $events -contains "uac" -and $events -contains "pause") "Cancelled UAC was mishandled" }
        "restart-elevated" { Assert-True ($scriptExit -ne 0 -and $events -notcontains "uac" -and $events -contains "pause") "Elevated failure prompted UAC again" }
        "bootstrap-preserve" {
            Assert-True ($scriptExit -eq 0) "Bootstrap failed: $failure"
            Assert-True ((Get-Content -LiteralPath (Join-Path $FixtureRoot ".env") -Raw).Trim() -eq "sentinel-existing-settings") "Bootstrap replaced .env"
            Assert-True (@($events | Where-Object { $_ -like '*-m pip install*' }).Count -eq 1) "Bootstrap did not install dependencies"
        }
    }
}
Write-Output ("POCKET_TEST_RESULT=" + (@{ scenario=$Scenario; events=@($global:PocketTestEvents); script_exit=$scriptExit; failure=$failure } | ConvertTo-Json -Compress))
