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

# usbipd --auto-attach is the only long-lived operation. If WSL restarts or
# USBIPD ends the session, retry only the same allowlisted device. This script
# never owns the ADB server.
while ($true) {
    try {
        if (-not (Test-ApprovedUsbDevicePresent)) {
            Write-BridgeEvent 'approved_usb_device_not_present; retrying'
            Start-Sleep -Seconds 15
            continue
        }
        & usbipd.exe bind --busid $BusId 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'usbipd bind failed' }
        Write-BridgeEvent 'starting_auto_attach_session'
        # usbipd-win 5.x accepts the WSL distribution as the optional value of
        # --wsl; it is not a separate --distribution option.  Keeping this
        # argv shape versioned prevents a silent auto-attach loop after an
        # usbipd upgrade.
        & usbipd.exe attach --wsl $Distro --busid $BusId --auto-attach --unplugged
        if ($LASTEXITCODE -ne 0) { throw 'usbipd auto-attach failed' }
        Write-BridgeEvent 'auto_attach_session_ended; retrying'
    }
    catch {
        # Device-specific details are deliberately not printed.
        Write-BridgeEvent 'bridge_operation_failed; retrying'
    }
    Start-Sleep -Seconds 15
}
