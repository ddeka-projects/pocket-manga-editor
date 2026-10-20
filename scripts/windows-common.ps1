#requires -Version 5.1
# Independent copy of the common Windows lifecycle used by the Pocket apps.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "app-settings.ps1")
$PocketRoot = Split-Path -Parent $PSScriptRoot

function Test-PocketAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-PocketTaskName {
    param([string]$TaskName)
    if ([string]::IsNullOrWhiteSpace($TaskName) -or [WildcardPattern]::ContainsWildcardCharacters($TaskName) -or $TaskName.Contains('"')) {
        throw "Use an exact scheduled task name without wildcard or quote characters."
    }
}

function Get-PocketPython {
    param([switch]$Windowless)
    $name = if ($Windowless) { "pythonw.exe" } else { "python.exe" }
    $python = Join-Path $PocketRoot ".venv\Scripts\$name"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Virtual environment missing: $python. Run scripts\bootstrap-windows.ps1 first."
    }
    return $python
}

function Invoke-PocketChecked {
    param([string]$FilePath, [string[]]$Arguments)
    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 can turn harmless native stderr into ErrorRecords.
        $ErrorActionPreference = "Continue"
        $output = @(& $FilePath @Arguments)
        $exitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    if ($exitCode -ne 0) { throw "Command failed with exit code ${exitCode}: $FilePath. Check the configuration or logs\server.log." }
    return $output
}

function Get-PocketSettings {
    $python = Get-PocketPython
    Push-Location -LiteralPath $PocketRoot
    try {
        $code = "import json; from $PocketPackage.config import load_settings; s=load_settings(); print(json.dumps(dict(host=s.host, port=s.port)))"
        $lines = @(Invoke-PocketChecked $python @("-B", "-c", $code))
        if ($lines.Count -eq 0) { throw "The app did not return its server configuration." }
        return ($lines[-1] | ConvertFrom-Json)
    }
    finally { Pop-Location }
}

function Get-PocketTask {
    param([string]$TaskName)
    Assert-PocketTaskName $TaskName
    try { return Get-ScheduledTask -TaskName $TaskName -TaskPath "\" -ErrorAction Stop }
    catch {
        if ($_.CategoryInfo.Category -eq [Management.Automation.ErrorCategory]::ObjectNotFound) { return $null }
        throw
    }
}

function Stop-PocketTask {
    param([string]$TaskName, [int]$TimeoutSeconds = 30)
    $task = Get-PocketTask $TaskName
    if ($null -eq $task) { return }
    if ([string]$task.State -in @("Running", "Queued")) {
        Write-Host "Stopping '$TaskName'..."
        Stop-ScheduledTask -TaskName $TaskName -TaskPath "\" -ErrorAction Stop
    }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    do {
        $task = Get-PocketTask $TaskName
        if ($null -eq $task) { throw "The task disappeared while stopping: $TaskName" }
        $state = [string]$task.State
        if ($state -in @("Ready", "Disabled")) { return }
        if ($state -notin @("Running", "Queued")) { throw "Cannot confirm '$TaskName' stopped (state: $state). No new instance was started." }
        if ($watch.Elapsed.TotalSeconds -ge $TimeoutSeconds) { throw "Timed out stopping '$TaskName'. No new instance was started." }
        Start-Sleep -Milliseconds 250
    } while ($true)
}

function Get-PocketHealth {
    param($Settings)
    $hostName = [string]$Settings.host
    if ($hostName -eq "0.0.0.0") { $hostName = "127.0.0.1" }
    if ($hostName -eq "::") { $hostName = "::1" }
    if ($hostName.Contains(":")) { $hostName = "[$hostName]" }
    $url = "http://${hostName}:$($Settings.port)/health"
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        $health = $response.Content | ConvertFrom-Json
        if ($response.StatusCode -eq 200 -and $health.ok -eq $true -and $health.app -eq $PocketPackage -and $health.instance_id) { return $health }
    }
    catch { }
    return $null
}

function Wait-PocketServer {
    param([string]$TaskName, $Settings, [string]$PreviousInstance = "", [int]$TimeoutSeconds = 90)
    Write-Host "Waiting for the server to become ready..."
    $watch = [Diagnostics.Stopwatch]::StartNew()
    do {
        $task = Get-PocketTask $TaskName
        if ($null -eq $task) { throw "The task disappeared while starting: $TaskName" }
        $state = [string]$task.State
        if ($state -eq "Disabled") { throw "The task is disabled: $TaskName. Enable it before restarting." }
        if ($state -eq "Running") {
            $health = Get-PocketHealth $Settings
            if ($null -ne $health -and [string]$health.instance_id -ne $PreviousInstance) {
                Start-Sleep -Milliseconds 500
                if ([string](Get-PocketTask $TaskName).State -eq "Running") { return }
            }
        }
        elseif ($watch.Elapsed.TotalSeconds -ge 3 -and $state -ne "Queued") {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath "\" -ErrorAction Stop
            throw "The server exited before becoming ready (task result: $($info.LastTaskResult)). See logs\server.log."
        }
        if ($watch.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            throw "Readiness was not confirmed within $TimeoutSeconds seconds (task state: $state). See logs\server.log; a large library may still be scanning."
        }
        Start-Sleep -Milliseconds 500
    } while ($true)
}

function Set-PocketFirewall {
    param([int]$Port)
    $name = "$PocketAppName (Private LAN)"
    Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction Stop
    New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private -RemoteAddress LocalSubnet -ErrorAction Stop | Out-Null
}

function Test-PocketAccessDenied {
    param($Record)
    if ($Record.CategoryInfo.Category -eq [Management.Automation.ErrorCategory]::PermissionDenied) { return $true }
    $exception = $Record.Exception
    while ($null -ne $exception) {
        if ($exception -is [UnauthorizedAccessException] -or $exception.HResult -eq -2147024891 -or $exception.HResult -eq -2147217405) { return $true }
        if ($exception -is [ComponentModel.Win32Exception] -and $exception.NativeErrorCode -eq 5) { return $true }
        if ($exception.PSObject.Properties["StatusCode"] -and [string]$exception.StatusCode -eq "AccessDenied") { return $true }
        $exception = $exception.InnerException
    }
    return $false
}

function Test-PocketCancelled {
    param($Record)
    $exception = $Record.Exception
    while ($null -ne $exception) {
        if ($exception -is [ComponentModel.Win32Exception] -and $exception.NativeErrorCode -eq 1223) { return $true }
        $exception = $exception.InnerException
    }
    return $false
}
