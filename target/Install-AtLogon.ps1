[CmdletBinding()]
param(
    [string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'config.json'),
    [string]$PendingPath = (Join-Path $PSScriptRoot 'pending.json'),
    [string]$TaskName = 'OC-ScriptedBootCapture'
)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$role = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $role.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run elevated on the target interactive desktop'
}
$cpu = Get-ItemPropertyValue 'HKLM:\HARDWARE\DESCRIPTION\System\CentralProcessor\0' ProcessorNameString
$board = Get-ItemPropertyValue 'HKLM:\HARDWARE\DESCRIPTION\System\BIOS' BaseBoardProduct
if ($cpu -notlike '*9950X3D*' -or $board -ne 'B850M AORUS ELITE WIFI6E ICE') { throw 'Target identity mismatch' }
foreach ($p in @($Python, $ConfigPath, (Join-Path $PSScriptRoot 'pipeline.py'))) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "Missing $p" }
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'Task exists; inspect it before replacing'
}
$script = Join-Path $PSScriptRoot 'pipeline.py'
$argument = "`"$script`" capture-pending --config `"$ConfigPath`" --pending `"$PendingPath`""
$action = New-ScheduledTaskAction -Execute $Python -Argument $argument -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Write-Output 'Installed for future logins only; task was not started. No credentials were stored.'
