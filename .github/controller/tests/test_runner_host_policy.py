from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WINDOWS = ROOT / "infra/runner-host/windows/mobile-proxy-usb-bridge.ps1"
WINDOWS_INSTALLER = ROOT / "infra/runner-host/windows/install-mobile-proxy-usb-bridge.ps1"
WATCHDOG = ROOT / "infra/runner-host/wsl/runner-transport-health.sh"
UNIT = ROOT / "infra/runner-host/wsl/mobile-proxy-runner-transport-health.service"
TIMER = ROOT / "infra/runner-host/wsl/mobile-proxy-runner-transport-health.timer"
ANDROID_USB_DROPIN = ROOT / "infra/runner-host/wsl/mobile-proxy-phone-runner-android-usb-permissions.conf"
ANDROID_USB_INSTALLER = ROOT / "infra/runner-host/wsl/install-runner-android-usb-permissions.sh"


def test_windows_bridge_has_one_allowlisted_usbipd_ownership_boundary() -> None:
    script = WINDOWS.read_text(encoding="utf-8").lower()
    assert "validatepattern('^\\d+-\\d+$')" in script
    assert "validatepattern('^[0-9a-fa-f]{4}:[0-9a-fa-f]{4}$')" in script
    assert "$usbipdexecutable = join-path $env:programfiles 'usbipd-win\\usbipd.exe'" in script
    assert "$wslexecutable = join-path $env:systemroot 'system32\\wsl.exe'" in script
    assert "& $wslexecutable --distribution $distro --exec /bin/true" in script
    assert "& $usbipdexecutable bind --busid $busid" not in script
    assert "& $usbipdexecutable state 2>&1" in script
    assert "convertfrom-json -erroraction stop" in script
    assert "if (-not (test-approvedusbdeviceattached))" in script
    assert "$rawattach = @(& $usbipdexecutable attach --wsl $distro --busid $busid 2>&1)" in script
    assert "$attachexitcode = $lastexitcode" in script
    assert "--auto-attach" not in script
    assert "mobile-proxy-usb-bridge.log" in script
    assert "65536" in script
    assert "exception text are deliberately never persisted" in script
    assert "exception.gettype().name" in script
    assert "scriptlinenumber" in script
    assert "write-bridgeevent 'bridge_started'" in script
    assert script.count("write-bridgeevent 'bridge_started'") == 1
    for category in ("inventory_unavailable", "bind_failed", "attach_failed", "wsl_unavailable", "unexpected_local_error"):
        assert category in script
    assert "attach --wsl --distribution" not in script
    for forbidden in ("adb get-state", "adb kill-server", "adb.exe", "udevadm", "usbipd.exe detach"):
        assert forbidden not in script


def test_windows_bridge_reduces_attach_output_to_bounded_categories() -> None:
    script = WINDOWS.read_text(encoding="utf-8").lower()
    assert "function get-usbipdattachfailurecategory" in script
    assert "$attachfailurecategory = get-usbipdattachfailurecategory -outputlines $rawattach" in script
    assert '$rawattach = $null' in script
    assert 'throw "usbipd attach failed:$attachfailurecategory"' in script
    assert "write-bridgeevent $rawattach" not in script
    assert "add-content -literalpath $logpath -value $rawattach" not in script
    for category in (
        "wsl2_unavailable",
        "wsl_support_missing",
        "usbipd_not_local_drive",
        "distro_missing",
        "distro_not_wsl2",
        "distro_not_running",
        "kernel_not_usbip_capable",
        "vhci_unavailable",
        "wsl_support_mount_failed",
        "usbip_client_unavailable",
        "host_address_unavailable",
        "networking_mode_unsupported",
        "firewall_blocked",
        "windows_device_busy",
        "usbipd_service_unavailable",
        "usbip_client_attach_failed",
        "unknown",
    ):
        assert category in script


def test_windows_installer_updates_only_existing_named_task() -> None:
    script = WINDOWS_INSTALLER.read_text(encoding="utf-8")
    assert "MobileProxyUsbBridge" in script
    assert "Get-ScheduledTask" in script
    assert "refusing to create" in script
    assert "New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest" in script
    assert "New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME" in script
    assert "Set-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Trigger $trigger" in script
    assert "-WindowStyle Hidden" in script
    assert "-LogonType S4U" not in script
    assert "-LogonType ServiceAccount" not in script
    assert "-NonInteractive" not in script
    assert "usbipd-win\\usbipd.exe" in script
    assert "Initial allowlisted USBIPD bind failed." in script
    assert "$ErrorActionPreference = 'Continue'" in script
    assert "$bindExitCode = $LASTEXITCODE" in script


def test_windows_installer_hardens_existing_bridge_task_lifetime() -> None:
    script = WINDOWS_INSTALLER.read_text(encoding="utf-8")
    assert "$settings = New-ScheduledTaskSettingsSet" in script
    assert "-RestartCount 3" in script
    assert "-RestartInterval (New-TimeSpan -Minutes 1)" in script
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    assert "-StartWhenAvailable" in script
    assert "-AllowStartIfOnBatteries" in script
    assert "-DontStopIfGoingOnBatteries" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-Settings $settings" in script
    assert script.count("New-ScheduledTaskTrigger") == 1
    assert "New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME" in script


def test_windows_installer_activates_replaced_payload_with_bounded_same_task_restart() -> None:
    script = WINDOWS_INSTALLER.read_text(encoding="utf-8")
    assert "$currentState = (Get-ScheduledTask -TaskName $TaskName).State" in script
    assert "if ($currentState -eq 'Running')" in script
    assert "Stop-ScheduledTask -TaskName $TaskName" in script
    assert "$stopDeadline = (Get-Date).AddSeconds(15)" in script
    assert "$startDeadline = (Get-Date).AddSeconds(15)" in script
    assert "Start-Sleep -Milliseconds 250" in script
    assert "Start-ScheduledTask -TaskName $TaskName" in script
    assert "did not stop within bounded activation window" in script
    assert "did not reach Running within bounded activation window" in script
    assert script.index("Stop-ScheduledTask -TaskName $TaskName") < script.index("Start-ScheduledTask -TaskName $TaskName")
    for forbidden in ("Stop-Process", "taskkill", "usbipd.exe detach", "adb.exe", "adb "):
        assert forbidden not in script


def test_watchdog_is_bounded_and_never_reconfigures_runner_or_phone() -> None:
    script = WATCHDOG.read_text(encoding="utf-8")
    assert "readonly STALE_SECONDS=300" in script
    assert "readonly FAILURE_THRESHOLD=3" in script
    assert "readonly COOLDOWN_SECONDS=900" in script
    assert "readonly MAX_RESTARTS_PER_HOUR=3" in script
    assert 'systemctl restart "$RUNNER_SERVICE"' in script
    for forbidden in ("config.sh", "registration", "usbipd", "adb ", "adb\n", "curl ", "iptables"):
        assert forbidden not in script.lower()


def test_watchdog_runs_as_a_root_owned_timer_with_hardening() -> None:
    unit = UNIT.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")
    assert "User=root" in unit
    assert "ProtectSystem=strict" in unit
    assert "OnUnitActiveSec=1min" in timer
    assert "Persistent=true" in timer


def test_android_usb_permission_dropin_is_runner_service_scoped() -> None:
    dropin = ANDROID_USB_DROPIN.read_text(encoding="utf-8")
    assert dropin == "[Service]\nSupplementaryGroups=plugdev\n"


def test_android_usb_permission_installer_is_least_privilege_and_bounded() -> None:
    script = ANDROID_USB_INSTALLER.read_text(encoding="utf-8")
    lower = script.lower()
    assert 'readonly RUNNER_SERVICE="mobile-proxy-phone-runner.service"' in script
    assert 'readonly REQUIRED_GROUP="plugdev"' in script
    assert 'readonly ACTIVATION_TIMEOUT_SECONDS=15' in script
    assert 'systemctl cat "${RUNNER_SERVICE}"' in script
    assert 'systemctl show --property=User --value "${RUNNER_SERVICE}"' in script
    assert 'runner service unexpectedly runs as root' in script
    assert 'install -m 0644 "${SOURCE_DROPIN}" "${DROPIN_PATH}"' in script
    assert 'systemctl daemon-reload' in script
    assert 'systemctl restart --no-block "${RUNNER_SERVICE}"' in script
    assert 'systemctl is-active --quiet "${RUNNER_SERVICE}"' in script
    assert 'systemctl show --property=MainPID --value "${RUNNER_SERVICE}"' in script
    assert '/proc/${main_pid}/status' in script
    assert 'runner service process did not acquire required supplementary group' in script
    for required_tool in ("awk", "cut"):
        assert f"command -v {required_tool}" in script
    for forbidden in (
        "adb ",
        "adb\n",
        "adb.exe",
        "udevadm",
        "chmod",
        "chown",
        "setfacl",
        "usermod",
        "gpasswd",
        "usbipd",
        "config.sh",
        "systemctl enable",
        "systemctl disable",
    ):
        assert forbidden not in lower
    for usb_identifier in ("idvendor", "idproduct", "busid", "serial="):
        assert usb_identifier not in lower
