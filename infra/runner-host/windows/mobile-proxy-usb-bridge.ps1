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
$UsbipdExecutable = Join-Path $env:ProgramFiles 'usbipd-win\usbipd.exe'
$WslExecutable = Join-Path $env:SystemRoot 'System32\wsl.exe'

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

function Get-UsbipdAttachFailureCategory {
    param([Parameter(Mandatory = $true)][object[]]$OutputLines)

    # usbipd output may contain device identifiers, distribution names and host
    # addresses. Keep it only in process memory, reduce it to one allowlisted
    # generic category, then discard it. Never persist the raw command output.
    $text = ($OutputLines | ForEach-Object { [string]$_ }) -join "`n"
    switch -Regex ($text) {
        'Windows Subsystem for Linux version 2 is not available' { return 'wsl2_unavailable' }
        'WSL support was not installed' { return 'wsl_support_missing' }
        "Option '--wsl' requires that this software is installed on a local drive" { return 'usbipd_not_local_drive' }
        'The WSL distribution .+ does not exist' { return 'distro_missing' }
        'selected WSL distribution is using (WSL 1|unsupported WSL)' { return 'distro_not_wsl2' }
        'selected WSL distribution is not running' { return 'distro_not_running' }
        'WSL kernel is not USBIP capable' { return 'kernel_not_usbip_capable' }
        "The 'modprobe' command is unavailable|Loading vhci_hcd failed" { return 'vhci_unavailable' }
        'Mounting .+ within WSL failed' { return 'wsl_support_mount_failed' }
        "Unable to run 'usbip' client tool" { return 'usbip_client_unavailable' }
        'Unable to determine host address' { return 'host_address_unavailable' }
        'Networking mode .+ is not supported' { return 'networking_mode_unsupported' }
        'A firewall appears to be blocking the connection' { return 'firewall_blocked' }
        'Device busy|appears to be used by Windows' { return 'windows_device_busy' }
        'service.+not running|server.+not running' { return 'usbipd_service_unavailable' }
        'Failed to attach device with busid' { return 'usbip_client_attach_failed' }
        default { return 'unknown' }
    }
}

function Get-BridgeFailureCategory {
    param([Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord)
    if ($ErrorRecord.Exception.Message -match '^usbipd attach failed:([a-z0-9_]+)$') {
        return "attach_$($Matches[1])"
    }
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
    $inventory = & $UsbipdExecutable list 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'usbipd inventory unavailable' }
    $escapedBusId = [regex]::Escape($BusId)
    $escapedHardwareId = [regex]::Escape($HardwareId)
    return [bool]($inventory | Where-Object {
        $_ -match "^\s*$escapedBusId\s+.*$escapedHardwareId"
    })
}

function Test-ApprovedUsbDeviceAttached {
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $rawState = & $UsbipdExecutable state 2>&1
    $stateExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    if ($stateExitCode -ne 0) { throw 'usbipd state unavailable' }

    try {
        $state = $rawState | ConvertFrom-Json -ErrorAction Stop
        $device = $state.Devices | Where-Object { $_.BusId -eq $BusId } | Select-Object -First 1
        return $null -ne $device -and -not [string]::IsNullOrWhiteSpace([string]$device.ClientIPAddress)
    }
    catch { throw 'usbipd state invalid' }
}

function Ensure-WslDistroRunning {
    & $WslExecutable --distribution $Distro --exec /bin/true 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'wsl distro unavailable' }
}

# Record only a generic process-lifetime marker. This intentionally contains
# no device, distribution, user, process, or host identity.
Write-BridgeEvent 'bridge_started'

# Keep an idempotent attach lease for precisely one allowlisted device. This
# avoids both usbipd's event-loop gap after WSL restarts and JSON state parsing
# differences between task-host PowerShell versions. This script never owns
# ADB, device detach, or any phone operation.
while ($true) {
    try {
        Ensure-WslDistroRunning
        if (-not (Test-ApprovedUsbDevicePresent)) {
            Write-BridgeEvent 'approved_usb_device_not_present; retrying'
            Start-Sleep -Seconds 15
            continue
        }
        if (-not (Test-ApprovedUsbDeviceAttached)) {
            $previousErrorAction = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            $rawAttach = @(& $UsbipdExecutable attach --wsl $Distro --busid $BusId 2>&1)
            $attachExitCode = $LASTEXITCODE
            $ErrorActionPreference = $previousErrorAction
            if ($attachExitCode -ne 0) {
                $attachFailureCategory = Get-UsbipdAttachFailureCategory -OutputLines $rawAttach
                $rawAttach = $null
                throw "usbipd attach failed:$attachFailureCategory"
            }
            $rawAttach = $null
            Write-BridgeEvent 'allowlisted_usb_attached_to_wsl'
        }
    }
    catch {
        # Device-specific details are deliberately not printed. The bounded
        # category plus exception type/line locates a task-host defect without
        # recording an exception message, command output, or device identity.
        $category = Get-BridgeFailureCategory $_
        $type = $_.Exception.GetType().Name -replace '[^A-Za-z0-9_]', '_'
        $line = [Math]::Max(0, $_.InvocationInfo.ScriptLineNumber)
        Write-BridgeEvent ("bridge_{0}_{1}_line{2}; retrying" -f $category, $type, $line)
    }
    Start-Sleep -Seconds 15
}
