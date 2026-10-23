[CmdletBinding()]
param(
    [string]$TaskName = "GarminPersonalSync",
    [string]$SettingsPath = (Join-Path (Split-Path $PSScriptRoot -Parent) ".garmin.sync.settings.json"),
    [string]$PythonPath = (Join-Path (Split-Path $PSScriptRoot -Parent) ".venv\Scripts\python.exe"),
    [switch]$Remove,
    [switch]$PrintXml
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed task: $TaskName"
    return
}

$SettingsPath = (Resolve-Path -LiteralPath $SettingsPath).Path
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$ScriptPath = Join-Path (Split-Path $PSScriptRoot -Parent) "garmin_sync.py"
if ($SettingsPath.Contains('"') -or $PythonPath.Contains('"') -or $ScriptPath.Contains('"')) {
    throw "Task paths must not contain double quotes."
}
& $PythonPath $ScriptPath --settings $SettingsPath --check-config
if ($LASTEXITCODE -ne 0) {
    throw "Settings validation failed; task was not registered."
}

$Arguments = '"' + $ScriptPath + '" --settings "' + $SettingsPath + '" --scheduled'
$Escape = { param([string]$Value) [System.Security.SecurityElement]::Escape($Value) }
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Start = [DateTime]::UtcNow.AddMinutes(1).ToString("yyyy-MM-ddTHH:mm:00Z")
$WindowlessPython = Join-Path (Split-Path $PythonPath -Parent) "pythonw.exe"
if (Test-Path -LiteralPath $WindowlessPython) {
    $TaskPython = $WindowlessPython
} else {
    $TaskPython = $PythonPath
}
$CommandXml = & $Escape $TaskPython
$ArgumentsXml = & $Escape $Arguments
$WorkingDirectoryXml = & $Escape (Split-Path $ScriptPath -Parent)
$UserXml = & $Escape $User

# A UTC minute heartbeat delegates DST and daily gating to Python's IANA timezone.
$Xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Garmin archive; Python gates daily runs at configured Seattle time.</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>$Start</StartBoundary><Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>$UserXml</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>$CommandXml</Command><Arguments>$ArgumentsXml</Arguments><WorkingDirectory>$WorkingDirectoryXml</WorkingDirectory></Exec>
  </Actions>
</Task>
"@

if ($PrintXml) {
    Write-Output $Xml
    return
}

Register-ScheduledTask -TaskName $TaskName -Xml $Xml -Force | Out-Null
Write-Output "Registered task: $TaskName"
Write-Output "A lightweight minute trigger checks the configured timezone. Downloads run once per due day."
Write-Output "The task runs while this Windows user is logged in; missed runs catch up after login."
