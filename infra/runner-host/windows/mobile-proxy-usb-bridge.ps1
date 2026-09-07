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
            & $UsbipdExecutable attach --wsl $Distro --busid $BusId *> $null
            $attachExitCode = $LASTEXITCODE
            $ErrorActionPreference = $previousErrorAction
            if ($attachExitCode -ne 0) { throw 'usbipd attach failed' }
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
