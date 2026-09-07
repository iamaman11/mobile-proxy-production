[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = 'MobileProxyUsbBridge',
    [string]$Destination = 'C:\ProgramData\MobileProxy\usb-bridge\mobile-proxy-usb-bridge.ps1'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)) { throw 'Run this installer from an elevated owner session.' }

$source = Join-Path $PSScriptRoot 'mobile-proxy-usb-bridge.ps1'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw 'Versioned USB bridge source was not found.' }
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) { throw "Existing task '$TaskName' was not found; refusing to create an unreviewed task." }

$usbipd = Join-Path $env:ProgramFiles 'usbipd-win\usbipd.exe'
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $usbipd bind --busid 3-2 *> $null
$bindExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
if ($bindExitCode -ne 0) { throw 'Initial allowlisted USBIPD bind failed.' }

$destinationDirectory = Split-Path -Parent $Destination
if ($PSCmdlet.ShouldProcess($Destination, 'Install versioned USB bridge script')) {
    New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $Destination -Force
}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $Destination
)
if ($PSCmdlet.ShouldProcess($TaskName, 'Update existing owner-interactive bridge task')) {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    Set-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Trigger $trigger | Out-Null
    Start-ScheduledTask -TaskName $TaskName
}
Write-Output 'mobile-proxy USB bridge installed; existing owner-logon task retained.'
