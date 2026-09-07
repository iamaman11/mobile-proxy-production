[CmdletBinding()]
param(
    [ValidatePattern('^\d+-\d+$')]
    [string]$BusId = '3-2',
    [ValidatePattern('^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}$')]
    [string]$HardwareId = '04e8:6860',
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$Distro = 'Ubuntu'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-BridgeEvent {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Output ("{0:u} mobile-proxy-usb-bridge {1}" -f (Get-Date).ToUniversalTime(), $Message)
}

function Test-ApprovedUsbDevicePresent {
    $inventory = & usbipd.exe list 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'usbipd inventory unavailable' }
    $escapedBusId = [regex]::Escape($BusId)
    $escapedHardwareId = [regex]::Escape($HardwareId)
    return [bool]($inventory | Where-Object {
        $_ -match "^\s*$escapedBusId\s+.*$escapedHardwareId"
    })
}

function Test-ApprovedUsbDeviceAttached {
    $rawState = & usbipd.exe state 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'usbipd state unavailable' }
    $device = (($rawState | ConvertFrom-Json).Devices | Where-Object {
        $_.BusId -eq $BusId
    } | Select-Object -First 1)
    return $null -ne $device -and -not [string]::IsNullOrWhiteSpace([string]$device.ClientIPAddress)
}

# usbipd auto-attach is not reliable across a full WSL shutdown: its Windows
# process can remain alive while the old client disappears. Poll only this
# allowlisted device's local USBIPD state and attach only when it is absent.
# This script never owns the ADB server or a device detach operation.
while ($true) {
    try {
        if (-not (Test-ApprovedUsbDevicePresent)) {
            Write-BridgeEvent 'approved_usb_device_not_present; retrying'
            Start-Sleep -Seconds 15
            continue
        }
        if (-not (Test-ApprovedUsbDeviceAttached)) {
            & usbipd.exe bind --busid $BusId 2>$null
            if ($LASTEXITCODE -ne 0) { throw 'usbipd bind failed' }
            & usbipd.exe attach --wsl $Distro --busid $BusId 2>$null
            if ($LASTEXITCODE -ne 0) { throw 'usbipd attach failed' }
            Write-BridgeEvent 'allowlisted_usb_attached_to_wsl'
        }
    }
    catch {
        # Device-specific details are deliberately not printed.
        Write-BridgeEvent 'bridge_operation_failed; retrying'
    }
    Start-Sleep -Seconds 15
}
