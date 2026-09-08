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
if ($PSCmdlet.ShouldProcess($TaskName, 'Update and activate existing owner-interactive bridge task')) {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew
    Set-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Trigger $trigger -Settings $settings | Out-Null

    # A long-lived PowerShell task does not reload a replaced .ps1 file. With
    # MultipleInstances=IgnoreNew, Start-ScheduledTask alone would leave an
    # already-running old bridge process in place. Replace only this same named
    # task instance, with bounded waits; never kill processes by name/PID and
    # never detach USB or invoke ADB here.
    $currentState = (Get-ScheduledTask -TaskName $TaskName).State
    if ($currentState -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName
        $stopDeadline = (Get-Date).AddSeconds(15)
        do {
            Start-Sleep -Milliseconds 250
            $currentState = (Get-ScheduledTask -TaskName $TaskName).State
        } while ($currentState -eq 'Running' -and (Get-Date) -lt $stopDeadline)
        if ($currentState -eq 'Running') {
            throw 'Existing USB bridge task did not stop within bounded activation window.'
        }
    }

    Start-ScheduledTask -TaskName $TaskName
    $startDeadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 250
        $currentState = (Get-ScheduledTask -TaskName $TaskName).State
    } while ($currentState -ne 'Running' -and (Get-Date) -lt $startDeadline)
    if ($currentState -ne 'Running') {
        throw 'Updated USB bridge task did not reach Running within bounded activation window.'
    }
}
Write-Output 'mobile-proxy USB bridge installed and activated; existing owner-logon task retained with bounded restart policy.'
