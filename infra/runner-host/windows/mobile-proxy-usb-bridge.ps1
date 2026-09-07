[CmdletBinding()]
param(
    [ValidatePattern('^\d+-\d+$')]
    [string]$BusId = '3-2',
    [ValidatePattern('^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}$')]
    [string]$HardwareId = '04e8:6860',
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$Distro = 'Ubuntu',
    [string]$LogPath = 'C:\ProgramData\MobileProxy\usb-bridge\mobile-proxy-usb-bridge.log'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-BridgeEvent {
    param([Parameter(Mandatory = $true)][string]$Message)
    $line = "{0:u} mobile-proxy-usb-bridge {1}" -f (Get-Date).ToUniversalTime(), $Message
    # Keep only bounded, generic events. Device inventory, command output,
    # identifiers, and exception text are deliberately never persisted.
    try {
        $directory = Split-Path -Parent $LogPath
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        Add-Content -LiteralPath $LogPath -Value $line -Encoding utf8
        if ((Get-Item -LiteralPath $LogPath).Length -gt 65536) {
            Get-Content -LiteralPath $LogPath -Tail 200 | Set-Content -LiteralPath $LogPath -Encoding utf8
        }
    }
    catch { }
    Write-Output $line
}

function Get-BridgeFailureCategory {
    param([Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord)
    switch -Regex ($ErrorRecord.Exception.Message) {
        '^usbipd inventory unavailable$' { return 'inventory_unavailable' }
        '^usbipd state unavailable$' { return 'state_unavailable' }
        '^usbipd bind failed$' { return 'bind_failed' }
        '^usbipd attach failed$' { return 'attach_failed' }
        '^wsl distro unavailable$' { return 'wsl_unavailable' }
        default { return 'unexpected_local_error' }
    }
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

function Ensure-WslDistroRunning {
    & wsl.exe --distribution $Distro --exec /bin/true 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'wsl distro unavailable' }
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
        Ensure-WslDistroRunning
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
        # Device-specific details are deliberately not printed. The bounded
        # category is enough to distinguish a bridge failure from ADB/phone.
        Write-BridgeEvent ("bridge_{0}; retrying" -f (Get-BridgeFailureCategory $_))
    }
    Start-Sleep -Seconds 15
}
